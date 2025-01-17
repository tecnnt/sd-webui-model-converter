import os.path
import tqdm
import torch
import safetensors.torch
import gradio as gr
from torch import Tensor
from modules import script_callbacks, shared
from modules import sd_models
from modules.ui import create_refresh_button

# position_ids in clip is int64. model_ema.num_updates is int32
dtypes_to_fp16 = {torch.float32, torch.float64, torch.bfloat16}
dtypes_to_bf16 = {torch.float32, torch.float64, torch.float16}


def conv_fp16(t: Tensor):
    return t.half() if t.dtype in dtypes_to_fp16 else t


def conv_bf16(t: Tensor):
    return t.bfloat16() if t.dtype in dtypes_to_bf16 else t


def conv_full(t):
    return t


_g_precision_func = {
    "full": conv_full,
    "fp32": conv_full,
    "fp16": conv_fp16,
    "bf16": conv_bf16,
}


def gr_show(visible=True):
    return {"visible": visible, "__type__": "update"}


def check_weight_type(k: str) -> str:
    if k.startswith("model.diffusion_model"):
        return "unet"
    elif k.startswith("first_stage_model"):
        return "vae"
    elif k.startswith("cond_stage_model"):
        return "clip"
    return "other"


def add_tab():
    with gr.Blocks(analytics_enabled=False) as ui:
        with gr.Row().style(equal_height=False):
            with gr.Column(variant='panel'):
                gr.HTML(value="<p>Converted checkpoints will be saved in your <b>checkpoint</b> directory.</p>")

                with gr.Row():
                    model_name = gr.Dropdown(sd_models.checkpoint_tiles(),
                                             elem_id="model_converter_model_name",
                                             label="Model")
                    create_refresh_button(model_name, sd_models.list_models,
                                          lambda: {"choices": sd_models.checkpoint_tiles()},
                                          "refresh_checkpoint_Z")

                custom_name = gr.Textbox(label="Custom Name (Optional)", elem_id="model_converter_custom_name")

                with gr.Row():
                    precision = gr.Radio(choices=["fp32", "fp16", "bf16"], value="fp32",
                                         label="Precision", elem_id="checkpoint_precision")
                    m_type = gr.Radio(choices=["disabled", "no-ema", "ema-only"], value="disabled",
                                      label="Pruning Methods")

                with gr.Row():
                    checkpoint_formats = gr.CheckboxGroup(choices=["ckpt", "safetensors"], value=["ckpt"],
                                                          label="Checkpoint Format", elem_id="checkpoint_format")
                    show_extra_options = gr.Checkbox(label="Show extra options", value=False)

                with gr.Row():
                    force_position_id = gr.Checkbox(label="Force CLIP position_id to int64 before convert", value=False)
                    fix_clip = gr.Checkbox(label="Fix clip", value=False)

                with gr.Row(visible=False) as extra_options:
                    specific_part_conv = ["copy", "convert", "delete"]
                    unet_conv = gr.Dropdown(specific_part_conv, value="convert", label="unet")
                    text_encoder_conv = gr.Dropdown(specific_part_conv, value="convert", label="text encoder")
                    vae_conv = gr.Dropdown(specific_part_conv, value="convert", label="vae")
                    others_conv = gr.Dropdown(specific_part_conv, value="convert", label="others")

                model_converter_convert = gr.Button(elem_id="model_converter_convert", label="Convert",
                                                    variant='primary')

            with gr.Column(variant='panel'):
                submit_result = gr.Textbox(elem_id="model_converter_result", show_label=False)

            show_extra_options.change(
                fn=lambda x: gr_show(x),
                inputs=[show_extra_options],
                outputs=[extra_options],
            )

            model_converter_convert.click(
                fn=do_convert,
                inputs=[
                    model_name,
                    checkpoint_formats,
                    precision, m_type, custom_name,
                    unet_conv,
                    text_encoder_conv,
                    vae_conv,
                    others_conv,
                    fix_clip,
                    force_position_id
                ],
                outputs=[submit_result]
            )

    return [(ui, "Model Converter", "model_converter")]


def load_model(path):
    if path.endswith(".safetensors"):
        m = safetensors.torch.load_file(path, device="cpu")
    else:
        m = torch.load(path, map_location="cpu")
    state_dict = m["state_dict"] if "state_dict" in m else m
    return state_dict


def fix_model(model, fix_clip=False, force_position_id=False):
    # code from model-toolkit
    nai_keys = {
        'cond_stage_model.transformer.embeddings.': 'cond_stage_model.transformer.text_model.embeddings.',
        'cond_stage_model.transformer.encoder.': 'cond_stage_model.transformer.text_model.encoder.',
        'cond_stage_model.transformer.final_layer_norm.': 'cond_stage_model.transformer.text_model.final_layer_norm.'
    }
    position_id_key = "cond_stage_model.transformer.text_model.embeddings.position_ids"
    for k in list(model.keys()):
        for r in nai_keys:
            if type(k) == str and k.startswith(r):
                new_key = k.replace(r, nai_keys[r])
                model[new_key] = model[k]
                del model[k]
                print(f"fixed novelai error key {k}")
                break

    if force_position_id and position_id_key in model:
        model[position_id_key] = model[position_id_key].to(torch.int64)

    if fix_clip:
        if position_id_key in model:
            correct = torch.Tensor([list(range(77))]).to(torch.int64)
            now = model[position_id_key].to(torch.int64)

            broken = correct.ne(now)
            broken = [i for i in range(77) if broken[0][i]]
            if len(broken) != 0:
                model[position_id_key] = correct
                print(f"fixed broken clip\n{broken}")
            else:
                print("clip in this model is fine, skip fixing...")

    return model


def do_convert(model, checkpoint_formats,
               precision, conv_type, custom_name,
               unet_conv, text_encoder_conv, vae_conv, others_conv,
               fix_clip, force_position_id):
    if model == "":
        return "Error: you must choose a model"
    if len(checkpoint_formats) == 0:
        return "Error: at least choose one model save format"

    model_info = sd_models.checkpoints_list[model]

    extra_opt = {
        "unet": unet_conv,
        "clip": text_encoder_conv,
        "vae": vae_conv,
        "other": others_conv
    }
    shared.state.begin()
    shared.state.job = 'model-convert'
    shared.state.textinfo = f"Loading {model_info.filename}..."
    print(f"Loading {model_info.filename}...")

    ok = {}
    state_dict = load_model(model_info.filename)
    fix_model(state_dict, fix_clip=fix_clip, force_position_id=force_position_id)

    conv_func = _g_precision_func[precision]

    def _hf(wk: str, t: Tensor):
        if not isinstance(t, Tensor):
            return
        weight_type = check_weight_type(wk)
        conv_t = extra_opt[weight_type]
        if conv_t == "convert":
            ok[wk] = conv_func(t)
        elif conv_t == "copy":
            ok[wk] = t
        elif conv_t == "delete":
            return

    print("Converting model...")

    if conv_type == "ema-only":
        for k in tqdm.tqdm(state_dict):
            ema_k = "___"
            try:
                ema_k = "model_ema." + k[6:].replace(".", "")
            except:
                pass
            if ema_k in state_dict:
                _hf(k, state_dict[ema_k])
                # print("ema: " + ema_k + " > " + k)
            elif not k.startswith("model_ema.") or k in ["model_ema.num_updates", "model_ema.decay"]:
                _hf(k, state_dict[k])
            #     print(k)
            # else:
            #     print("skipped: " + k)
    elif conv_type == "no-ema":
        for k, v in tqdm.tqdm(state_dict.items()):
            if "model_ema." not in k:
                _hf(k, v)
    else:
        for k, v in tqdm.tqdm(state_dict.items()):
            _hf(k, v)

    output = ""
    ckpt_dir = shared.cmd_opts.ckpt_dir or sd_models.model_path
    save_name = f"{model_info.model_name}-{precision}"
    if conv_type != "disabled":
        save_name += f"-{conv_type}"

    if fix_clip:
        save_name += f"-clip-fix"

    if custom_name != "":
        save_name = custom_name

    for fmt in checkpoint_formats:
        ext = ".safetensors" if fmt == "safetensors" else ".ckpt"
        _save_name = save_name + ext

        save_path = os.path.join(ckpt_dir, _save_name)
        print(f"Saving to {save_path}...")

        if fmt == "safetensors":
            safetensors.torch.save_file(ok, save_path)
        else:
            torch.save({"state_dict": ok}, save_path)
        output += f"Checkpoint saved to {save_path}\n"

    shared.state.end()
    return output[:-1]


script_callbacks.on_ui_tabs(add_tab)

