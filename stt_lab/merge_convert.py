"""LoRA 어댑터를 Whisper large-v3에 합치고 faster-whisper(CTranslate2 float16) 형식으로 바꾼다.

사용: PYTHONPATH=pylib ~/voiceguard/.venv/bin/python -X utf8 scripts/merge_convert.py <어댑터 폴더> <출력 폴더>
출력: <출력>/hf (합친 transformers 모델), <출력>/ct2 (WhisperModel("<출력>/ct2")로 바로 쓰는 폴더)
"""
import os
import sys
from pathlib import Path

import ctranslate2
import torch
from peft import PeftModel
from transformers import WhisperForConditionalGeneration, WhisperProcessor

BASE = os.environ.get("BASE", "openai/whisper-large-v3")
adapter, out = Path(sys.argv[1]), Path(sys.argv[2])
if (adapter / "adapter_config.json").exists():  # LoRA 어댑터
    base = WhisperForConditionalGeneration.from_pretrained(BASE, torch_dtype=torch.float16)
    model = PeftModel.from_pretrained(base, str(adapter)).merge_and_unload()
else:  # 전체 미세조정 체크포인트
    model = WhisperForConditionalGeneration.from_pretrained(str(adapter), torch_dtype=torch.float16)
model.config.apply_spec_augment = False
model.config.use_cache = True
hf = out / "hf"
model.save_pretrained(hf, safe_serialization=True)
WhisperProcessor.from_pretrained(BASE).save_pretrained(hf)
conv = ctranslate2.converters.TransformersConverter(str(hf), copy_files=["tokenizer.json", "preprocessor_config.json"])
conv.convert(str(out / "ct2"), quantization="float16", force=True)  # GPU용
conv.convert(str(out / "ct2_int8"), quantization="int8", force=True)  # CPU(노트북)용
print("saved", out / "ct2", out / "ct2_int8")
