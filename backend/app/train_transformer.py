"""2차 탐지 후보: 한국어 사전학습 언어모델(KoELECTRA) fine-tuning.

TF-IDF 베이스라인의 문맥 이해 한계(대출사기 vs 정상 대출상담의 어휘 근접성)를
문맥 이해형 모델로 넘어설 수 있는지 검증한다. 베이스라인과 동일한 80/20 분할(seed 42)로
공정 비교하며, Accuracy/Precision/Recall/F1을 함께 출력한다.

실행:
    .venv/bin/python -m backend.app.train_transformer                    # 기본 KoELECTRA
    MODEL=klue/bert-base .venv/bin/python -m backend.app.train_transformer
    # 통화 앞부분·중간 구간만 잘라 넣는 증강을 켜고, 후보 모델을 따로 저장
    PREFIX_AUG=3 KOELECTRA_MODEL_DIR=models/koelectra_candidate .venv/bin/python -m backend.app.train_transformer

FP16: CUDA에서만 활성화한다. MPS(Apple Silicon)는 fp16 mixed precision 학습이
불안정하므로 자동으로 끈다 (추론 속도에는 영향 없음).
"""
import json
import os
import random
import re

import numpy as np
import torch
from sklearn.metrics import accuracy_score, precision_recall_fscore_support
from transformers import (
    AutoModelForSequenceClassification,
    AutoTokenizer,
    Trainer,
    TrainingArguments,
)

from backend.app.dataset_split import split_cases
from backend.app.paths import KOELECTRA_MODEL_DIR, TRAINING_DATA_PATH
from backend.app.text_normalize import normalize_text, save_preprocess_config


DATA_PATH = TRAINING_DATA_PATH
MODEL_NAME = os.environ.get("MODEL", "monologg/koelectra-base-v3-discriminator")
OUT_DIR = KOELECTRA_MODEL_DIR
MAX_LEN = 256          # 피싱 신호는 통화 초중반에 몰려 있어 256으로 절단해도 대부분 포착
SEED = 42
EPOCHS = int(os.environ.get("EPOCHS", "3"))
# 통화 하나당 추가로 만드는 "잘린 통화" 수. 0이면 증강 없음(기존 방식).
# 실시간에서는 통화 앞부분이나 최근 몇 턴만 보고 판단하므로, 전체 통화만 학습하면
# 초반 점수가 불안정하다. 학습 통화를 임의 지점에서 잘라 같은 라벨로 함께 학습한다.
PREFIX_AUG = int(os.environ.get("PREFIX_AUG", "0"))
WINDOW_TURNS = 10        # 런타임 최근 구간 채점(koelectra_scorer.WINDOW)과 같은 길이
# 1이면 문장부호를 지우고 학습한다(출처별 전사 말투 차이를 줄임). 추론도 같은 전처리를 따른다.
NORMALIZE = os.environ.get("NORMALIZE", "0") == "1"
MIN_PHISHING_TURNS = 2   # 사기 통화를 이보다 짧게 자르면 사기 신호가 없는 조각에 사기 라벨이 붙는다
# 출처별 반복(학습 쪽만). 예: OVERSAMPLE="normal_kisa118=2,normal_twin=2" → id가 그 접두어로 시작하는 학습 통화를
# 2배로 넣는다(반복분은 앞부분 증강에서 다른 지점으로 잘린다). 클래스 가중 손실은 정상 전체의 무게를 고정하므로,
# 정상 데이터를 크게 늘리면 기존의 어려운 정상 사례(KISA 상담, 쌍둥이 통화)의 몫이 줄어드는 것을 되돌릴 때 쓴다.
OVERSAMPLE = {
    prefix.strip(): int(factor)
    for prefix, factor in (item.split("=") for item in os.environ.get("OVERSAMPLE", "").split(",") if "=" in item)
}


def _join(turns):
    return " ".join(t["text"] for t in turns)


def augment_case(case, rng, count):
    """통화 하나에서 앞부분(prefix)과 중간 구간(window)을 잘라 학습 텍스트를 추가로 만든다."""
    turns = case["turns"]
    minimum = MIN_PHISHING_TURNS if case["label"] == 1 else 1
    if len(turns) <= minimum:
        return []
    texts = []
    for i in range(count):
        if i % 2 == 0:
            end = rng.randint(minimum, len(turns) - 1)
            texts.append(_join(turns[:end]))
        else:
            length = rng.randint(minimum, min(WINDOW_TURNS, len(turns) - 1))
            start = rng.randint(0, len(turns) - length)
            texts.append(_join(turns[start:start + length]))
    return texts


def load_split():
    """통화 단위로 80/20 분할한다(SPLIT=hash면 group 해시 분할). 증강은 학습 쪽에만 적용한다."""
    with open(DATA_PATH, encoding="utf-8") as f:
        cases = json.load(f)["cases"]
    train_cases, eval_cases = split_cases(cases)
    if OVERSAMPLE:
        extra = []
        for case in train_cases:
            factor = next((f for prefix, f in OVERSAMPLE.items() if case["id"].startswith(prefix)), 1)
            extra.extend([case] * (factor - 1))
        print(f"출처별 반복(OVERSAMPLE={OVERSAMPLE}): 학습 통화 {len(extra)}건 추가")
        train_cases = train_cases + extra

    X_tr = [_join(c["turns"]) for c in train_cases]
    y_tr = [c["label"] for c in train_cases]
    if PREFIX_AUG > 0:
        rng = random.Random(SEED)
        for c in train_cases:
            extra = augment_case(c, rng, PREFIX_AUG)
            X_tr.extend(extra)
            y_tr.extend([c["label"]] * len(extra))
    X_te = [_join(c["turns"]) for c in eval_cases]
    y_te = [c["label"] for c in eval_cases]
    if NORMALIZE:
        X_tr = [normalize_text(text) for text in X_tr]
        X_te = [normalize_text(text) for text in X_te]
    return X_tr, X_te, y_tr, y_te


class WeightedTrainer(Trainer):
    """클래스 불균형(피싱 소수) 대응: 피싱 오분류에 더 큰 손실 가중치."""

    def __init__(self, class_weights, **kwargs):
        super().__init__(**kwargs)
        self.class_weights = class_weights

    def compute_loss(self, model, inputs, return_outputs=False, **kwargs):
        labels = inputs.pop("labels")
        outputs = model(**inputs)
        loss_fct = torch.nn.CrossEntropyLoss(
            weight=self.class_weights.to(outputs.logits.device)
        )
        loss = loss_fct(outputs.logits, labels)
        return (loss, outputs) if return_outputs else loss


def compute_metrics(pred):
    labels = pred.label_ids
    preds = pred.predictions.argmax(-1)
    acc = accuracy_score(labels, preds)
    # 피싱(1) 클래스 기준 지표
    p, r, f1, _ = precision_recall_fscore_support(
        labels, preds, labels=[1], average="binary", zero_division=0
    )
    return {"accuracy": acc, "precision": p, "recall": r, "f1": f1}


class DS(torch.utils.data.Dataset):
    def __init__(self, enc, labels):
        self.enc = enc
        self.labels = labels

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, i):
        item = {k: v[i] for k, v in self.enc.items()}
        item["labels"] = torch.tensor(self.labels[i])
        return item


def main():
    torch.manual_seed(SEED)
    X_tr, X_te, y_tr, y_te = load_split()
    print(f"모델: {MODEL_NAME}")
    print(f"학습 {len(X_tr)}건 / 평가 {len(X_te)}건 (평가셋 피싱 {sum(y_te)}건)")

    device = "cuda" if torch.cuda.is_available() else ("mps" if torch.backends.mps.is_available() else "cpu")
    use_fp16 = torch.cuda.is_available()  # MPS/CPU에서는 fp16 끔
    print(f"디바이스: {device} / fp16: {use_fp16}\n")

    tok = AutoTokenizer.from_pretrained(MODEL_NAME)
    enc_tr = tok(X_tr, truncation=True, padding=True, max_length=MAX_LEN, return_tensors="pt")
    enc_te = tok(X_te, truncation=True, padding=True, max_length=MAX_LEN, return_tensors="pt")

    # class_weight='balanced'와 동일한 계산
    n = len(y_tr)
    w0 = n / (2 * (n - sum(y_tr)))
    w1 = n / (2 * sum(y_tr))
    class_weights = torch.tensor([w0, w1], dtype=torch.float)

    model = AutoModelForSequenceClassification.from_pretrained(MODEL_NAME, num_labels=2)

    args = TrainingArguments(
        output_dir=str(OUT_DIR),
        num_train_epochs=EPOCHS,
        per_device_train_batch_size=16,
        per_device_eval_batch_size=32,
        learning_rate=2e-5,
        warmup_ratio=0.1,
        weight_decay=0.01,
        fp16=use_fp16,
        eval_strategy="epoch",
        save_strategy="epoch",
        load_best_model_at_end=True,
        metric_for_best_model="f1",
        logging_steps=20,
        report_to="none",
        seed=SEED,
    )

    trainer = WeightedTrainer(
        class_weights=class_weights,
        model=model,
        args=args,
        train_dataset=DS(enc_tr, y_tr),
        eval_dataset=DS(enc_te, y_te),
        compute_metrics=compute_metrics,
    )
    trainer.train()

    print("\n=== 최종 평가 (best model) ===")
    metrics = trainer.evaluate()
    for k in ["eval_accuracy", "eval_precision", "eval_recall", "eval_f1"]:
        print(f"  {k.replace('eval_', ''):10} {metrics[k] * 100:.2f}%")

    trainer.save_model(str(OUT_DIR))
    tok.save_pretrained(str(OUT_DIR))
    save_preprocess_config(OUT_DIR, NORMALIZE)
    print(f"\n모델 저장: {OUT_DIR}")


if __name__ == "__main__":
    main()
