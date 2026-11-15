"""Whisper large-v3 LoRA 추가 학습 (AI Hub 민원 콜센터 음성 + 대본).

데이터: meta/ds_train.jsonl, meta/ds_dev.jsonl (build_dataset.py)
- 예제 A: [ko][transcribe][notimestamps] 문장 [eot]
- 예제 B: 시각 토큰 있음 → [ko][transcribe] <|t|> 문장 <|t|> ... [eot] / 없음 → [notimestamps] 문장들 [eot]
  ([startoftranscript]는 모델이 labels를 오른쪽으로 밀어 decoder 입력을 만들 때 앞에 붙인다)
환경 변수: OUT, EPOCHS(1), MAX_STEPS(0=EPOCHS 기준), LR(3e-4), BATCH(16), ACCUM(2), LORA_R(32), MEM_FRAC(0.4),
          WORKERS(12), EVAL_EVERY(400), SEED(0), SPEC_AUG(1)
사용: PYTHONPATH=pylib ~/voiceguard/.venv/bin/python -X utf8 scripts/train_lora.py
"""
import json
import math
import os
import random
import re
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from peft import LoraConfig, get_peft_model
from torch.utils.data import DataLoader, Dataset
from transformers import WhisperForConditionalGeneration, WhisperProcessor

ROOT = Path.home() / "voiceguard_ft"
BASE = os.environ.get("BASE", "openai/whisper-large-v3")  # small·turbo도 같은 스크립트로
FULL_FT = os.environ.get("FULL_FT", "0") == "1"  # 1이면 LoRA 대신 전체 미세조정(작은 모델용)
env = os.environ.get
OUT = Path(env("OUT", str(ROOT / "runs" / "lora_r32")))
EPOCHS = float(env("EPOCHS", "1"))
MAX_STEPS = int(env("MAX_STEPS", "0"))
LR = float(env("LR", "3e-4"))
BATCH = int(env("BATCH", "16"))
ACCUM = int(env("ACCUM", "2"))
LORA_R = int(env("LORA_R", "32"))
MEM_FRAC = float(env("MEM_FRAC", "0.4"))
WORKERS = int(env("WORKERS", "12"))
EVAL_EVERY = int(env("EVAL_EVERY", "400"))
SEED = int(env("SEED", "0"))
SPEC_AUG = env("SPEC_AUG", "1") == "1"
SR = 16000
MAX_LABEL = 440

OUT.mkdir(parents=True, exist_ok=True)
random.seed(SEED)
np.random.seed(SEED)
torch.manual_seed(SEED)
torch.cuda.set_per_process_memory_fraction(MEM_FRAC)
torch.backends.cuda.matmul.allow_tf32 = True
log_f = open(OUT / "train_log.jsonl", "a", encoding="utf-8")


def log(**kw):
    kw["time"] = time.strftime("%H:%M:%S")
    print(json.dumps(kw, ensure_ascii=False), flush=True)
    log_f.write(json.dumps(kw, ensure_ascii=False) + "\n")
    log_f.flush()


proc = WhisperProcessor.from_pretrained(BASE)
tok, fe = proc.tokenizer, proc.feature_extractor
tid = tok.convert_tokens_to_ids
SOT, KO, TR, NOTS = tid("<|startoftranscript|>"), tid("<|ko|>"), tid("<|transcribe|>"), tid("<|notimestamps|>")
N_FRAMES = 3000
EOT = tok.eos_token_id
TS0 = tid("<|0.00|>")
assert TS0 != tok.unk_token_id and tid("<|30.00|>") == TS0 + 1500


def ts_id(t):
    return TS0 + int(round(min(max(t, 0.0), 30.0) / 0.02))


def text_ids(t):
    return tok(" " + t.strip(), add_special_tokens=False).input_ids


def ts_body(segs):
    """시각 토큰 방식 본문. CTranslate2 추론 규칙처럼 시각이 줄지 않게(시작 >= 앞 끝, 끝 > 시작) 맞춘다."""
    body, last = [], TS0
    for s in segs:
        a = max(ts_id(s["s"]), last)
        b = max(ts_id(s["e"]), a + 1)
        body += [a] + text_ids(s["text"]) + [b]
        last = b
    return body


def make_labels(ex):
    if ex["kind"] == "A":
        if ex.get("ts"):
            body = ts_body([{"s": ex["ts_s"], "e": ex["ts_e"], "text": ex["text"]}])
        else:
            body = [NOTS] + text_ids(ex["text"])
    elif ex.get("ts"):
        body = ts_body(ex["segs"])
    else:
        body = [NOTS] + text_ids(" ".join(s["text"] for s in ex["segs"]))
    return [KO, TR] + body + [EOT]


def ref_text(ex):
    return ex["text"] if ex["kind"] == "A" else " ".join(s["text"] for s in ex["segs"])


def load(path):
    rows = [json.loads(l) for l in open(path, encoding="utf-8")]
    keep = [r for r in rows if len(make_labels(r)) <= MAX_LABEL and r["e"] - r["s"] <= 30.0]
    return keep, len(rows) - len(keep)


class DS(Dataset):
    def __init__(self, rows):
        self.rows = rows

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, i):
        ex = self.rows[i]
        audio, _ = sf.read(ex["path"], start=int(ex["s"] * SR), stop=int(ex["e"] * SR), dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        # faster-whisper와 같은 입력: 패딩 없이 log-mel을 만든 뒤(정규화도 실제 음성 기준) 3000프레임까지 0으로 채운다
        feats = fe(audio, sampling_rate=SR, padding="longest", return_tensors="np").input_features[0]
        if feats.shape[1] < N_FRAMES:
            feats = np.pad(feats, ((0, 0), (0, N_FRAMES - feats.shape[1])))
        feats = feats[:, :N_FRAMES]
        return torch.from_numpy(np.ascontiguousarray(feats)), torch.tensor(make_labels(ex), dtype=torch.long)


def collate(batch):
    """decoder 입력을 직접 만든다: [SOT, ko, transcribe, ...]. 첫 위치(<|ko|> 예측)는 손실에서 뺀다.
    그 위치의 분포에서 faster-whisper가 '말 없음' 확률을 읽으므로, 말만 있는 데이터로 그 값을 흔들지 않게 한다."""
    feats = torch.stack([b[0] for b in batch])
    width = max(len(b[1]) for b in batch)
    labels = torch.full((len(batch), width), -100, dtype=torch.long)
    dec = torch.full((len(batch), width), EOT, dtype=torch.long)
    for i, (_, lab) in enumerate(batch):
        labels[i, : len(lab)] = lab
        dec[i, 0] = SOT
        dec[i, 1: len(lab)] = lab[:-1]
    labels[:, 0] = -100
    return feats, dec, labels


_norm = re.compile(r"[^0-9A-Za-z가-힣]")


def cer(ref, hyp):
    r, h = _norm.sub("", ref), _norm.sub("", hyp)
    if not r:
        return 0.0
    prev = list(range(len(h) + 1))
    for i, rc in enumerate(r, 1):
        cur = [i] + [0] * len(h)
        for j, hc in enumerate(h, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rc != hc))
        prev = cur
    return prev[-1] / len(r)


DS_DIR = Path(env("DS_DIR", str(ROOT / "meta")))  # 연기 시험(smoke test) 때 다른 폴더를 가리킨다
train_rows, dropped_train = load(DS_DIR / "ds_train.jsonl")
dev_rows, _ = load(DS_DIR / "ds_dev.jsonl")
rng = random.Random(1)
dev_loss_rows = rng.sample(dev_rows, min(400, len(dev_rows)))
_dev_a = [r for r in dev_rows if r["kind"] == "A"]
_dev_b = [r for r in dev_rows if r["kind"] == "B"]
dev_cer_rows = rng.sample(_dev_a, min(200, len(_dev_a))) + rng.sample(_dev_b, min(100, len(_dev_b)))  # 짧은 문장 + 긴 묶음
log(event="data", train=len(train_rows), dropped_long=dropped_train, dev=len(dev_rows),
    hours=round(sum(r["e"] - r["s"] for r in train_rows) / 3600, 1))

model = WhisperForConditionalGeneration.from_pretrained(BASE, torch_dtype=torch.float32 if FULL_FT else torch.bfloat16)
model.config.forced_decoder_ids = None
model.generation_config.forced_decoder_ids = None
if SPEC_AUG:
    model.config.apply_spec_augment = True
    model.config.mask_time_prob = 0.05
    model.config.mask_time_length = 10
    model.config.mask_feature_prob = 0.02
    model.config.mask_feature_length = 10
model.config.use_cache = False
if os.environ.get("GC", "1") == "1":  # 메모리가 넉넉하면 GC=0으로 꺼서 재계산을 없앤다(더 빠름)
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
lora = LoraConfig(r=LORA_R, lora_alpha=2 * LORA_R, lora_dropout=0.05, bias="none",
                  target_modules=["q_proj", "k_proj", "v_proj", "out_proj", "fc1", "fc2"])
if FULL_FT:
    for p_ in model.parameters():
        p_.requires_grad = True
else:
    model = get_peft_model(model, lora)
trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
total_params = sum(p.numel() for p in model.parameters())
log(event="model", trainable=trainable, total=total_params)
model.cuda()

params = [p for p in model.parameters() if p.requires_grad]
opt = torch.optim.AdamW(params, lr=LR, weight_decay=0.01, betas=(0.9, 0.98))
loader = DataLoader(DS(train_rows), batch_size=BATCH, shuffle=True, num_workers=WORKERS, collate_fn=collate,
                    pin_memory=True, drop_last=True, persistent_workers=True, prefetch_factor=4)
steps_per_epoch = len(loader) // ACCUM
TOTAL = MAX_STEPS or max(1, int(steps_per_epoch * EPOCHS))
WARMUP = min(200, max(10, TOTAL // 20))


def lr_at(step):
    if step < WARMUP:
        return LR * (step + 1) / WARMUP
    progress = (step - WARMUP) / max(1, TOTAL - WARMUP)
    return LR * (0.1 + 0.9 * 0.5 * (1 + math.cos(math.pi * min(1.0, progress))))


@torch.no_grad()
def evaluate(step):
    model.eval()
    model.config.use_cache = True
    dl = DataLoader(DS(dev_loss_rows), batch_size=BATCH, num_workers=4, collate_fn=collate)
    losses = []
    for x, d, y in dl:
        with torch.autocast("cuda", dtype=torch.bfloat16):
            losses.append(model(input_features=x.cuda().to(torch.bfloat16), decoder_input_ids=d.cuda(),
                                labels=y.cuda()).loss.item())
    dl = DataLoader(DS(dev_cer_rows), batch_size=BATCH, num_workers=4, collate_fn=collate)
    errs, refs_len, k = 0.0, 0, 0
    for x, _d, _y in dl:
        with torch.autocast("cuda", dtype=torch.bfloat16):
            # 서비스처럼 시각 토큰 방식으로 푼다(faster-whisper 기본값)
            out = model.generate(input_features=x.cuda().to(torch.bfloat16), language="ko", task="transcribe",
                                 return_timestamps=True, max_new_tokens=220, num_beams=1)
        hyps = tok.batch_decode(out, skip_special_tokens=True)
        for hyp in hyps:
            hyp = re.sub(r"<\|[^|]*\|>", "", hyp)
            ref = ref_text(dev_cer_rows[k])
            n = len(_norm.sub("", ref))
            errs += cer(ref, hyp) * n
            refs_len += n
            k += 1
    model.config.use_cache = False
    model.train()
    result = {"dev_loss": round(float(np.mean(losses)), 4), "dev_cer": round(errs / max(refs_len, 1), 4)}
    log(event="eval", step=step, **result)
    return result


log(event="start", total_steps=TOTAL, steps_per_epoch=steps_per_epoch, warmup=WARMUP, batch=BATCH, accum=ACCUM, lr=LR)
best = evaluate(0)
best["step"] = 0
step, micro, t0, seen = 0, 0, time.time(), 0
model.train()
done = False
while not done:
    for x, d, y in loader:
        x = x.cuda(non_blocking=True).to(torch.bfloat16)
        d = d.cuda(non_blocking=True)
        y = y.cuda(non_blocking=True)
        with torch.autocast("cuda", dtype=torch.bfloat16):
            loss = model(input_features=x, decoder_input_ids=d, labels=y).loss / ACCUM
        loss.backward()
        micro += 1
        seen += x.shape[0]
        if micro % ACCUM:
            continue
        for g in opt.param_groups:
            g["lr"] = lr_at(step)
        grad_norm = torch.nn.utils.clip_grad_norm_(params, 1.0)
        opt.step()
        opt.zero_grad(set_to_none=True)
        step += 1
        if step % 20 == 0:
            el = time.time() - t0
            log(event="train", step=step, loss=round(loss.item() * ACCUM, 4), grad=round(float(grad_norm), 3),
                lr=round(lr_at(step), 7), samples_per_s=round(seen / el, 1),
                mem_gb=round(torch.cuda.max_memory_allocated() / 1e9, 1), eta_min=round((TOTAL - step) * el / step / 60, 1))
        if step % EVAL_EVERY == 0 or step == TOTAL:
            res = evaluate(step)
            improved = res["dev_cer"] < best["dev_cer"]
            if not FULL_FT or improved or step == TOTAL:  # 전체 미세조정은 무거워서 좋아졌을 때·마지막만 저장
                model.save_pretrained(OUT / f"step{step}")
            if improved:
                best = {**res, "step": step}
                (OUT / "best.json").write_text(json.dumps(best), encoding="utf-8")
        if step >= TOTAL:
            done = True
            break
log(event="done", best=best, minutes=round((time.time() - t0) / 60, 1))
