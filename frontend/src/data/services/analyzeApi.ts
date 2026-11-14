/**
 * 통화 외 입력 분석 API: 문자·메신저 메시지(스미싱), 문서 이미지(피싱용 위조 문서).
 * 백엔드 POST /analyze/message, /analyze/document (multipart).
 */
import { Platform } from 'react-native';
import { BASE_URL_HTTP } from '@/core/config/env';

export interface PickedImage {
  uri: string;
  name: string;
  mime?: string;
}

export interface CheckFlag {
  code: string;
  label: string;
}

interface CheckBase {
  is_phishing: boolean;
  risk_score: number;
  risk_level: 'low' | 'medium' | 'high';
  explanation: string;
  recommended_actions: string[];
}

export interface MessageCheck extends CheckBase {
  category: string;
  category_label: string;
  red_flags: CheckFlag[];
  extracted_text: string;
  urls: { url: string; host: string; verdict: 'suspicious' | 'unknown' | 'likely_official'; reasons: string[] }[];
  phones: string[];
}

export interface DocumentCheck extends CheckBase {
  readable: boolean;
  doc_type: string;
  claimed_issuer: string;
  flags: CheckFlag[];
  evidence: string[];
  disclaimer: string;
}

const MAX_SIDE = 1600;

/** 휴대폰 사진은 수 MB라서 웹에서는 긴 변 1600px JPEG으로 줄여 보낸다(서버 제한 4MB). */
async function shrinkOnWeb(blob: Blob): Promise<Blob> {
  if (typeof document === 'undefined' || typeof createImageBitmap === 'undefined') return blob;
  try {
    const bitmap = await createImageBitmap(blob);
    const scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height));
    if (scale === 1 && blob.size < 1_500_000) return blob;
    const canvas = document.createElement('canvas');
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    canvas.getContext('2d')?.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    return await new Promise<Blob>((resolve) => canvas.toBlob((out) => resolve(out ?? blob), 'image/jpeg', 0.85));
  } catch {
    return blob;
  }
}

async function appendImage(form: FormData, image: PickedImage): Promise<void> {
  if (Platform.OS === 'web') {
    const source = await fetch(image.uri);
    const blob = await shrinkOnWeb(await source.blob());
    form.append('image', new File([blob], image.name || 'image.jpg', { type: blob.type || image.mime || 'image/jpeg' }));
    return;
  }
  // 네이티브: React Native의 FormData는 {uri, name, type} 객체를 파일로 보낸다.
  form.append('image', { uri: image.uri, name: image.name || 'image.jpg', type: image.mime || 'image/jpeg' } as unknown as Blob);
}

async function post<T>(path: string, form: FormData): Promise<T> {
  const res = await fetch(`${BASE_URL_HTTP}${path}`, { method: 'POST', body: form });
  if (!res.ok) {
    let detail = '';
    try {
      detail = String((await res.json()).detail ?? '');
    } catch {
      /* 본문이 JSON이 아니면 상태 코드만 보여준다 */
    }
    throw new Error(detail || `분석에 실패했습니다. (${res.status})`);
  }
  return (await res.json()) as T;
}

export async function checkMessage(text: string, image?: PickedImage | null): Promise<MessageCheck> {
  const form = new FormData();
  form.append('text', text);
  if (image) await appendImage(form, image);
  return post<MessageCheck>('/analyze/message', form);
}

export async function checkDocument(image: PickedImage, note = ''): Promise<DocumentCheck> {
  const form = new FormData();
  await appendImage(form, image);
  form.append('note', note);
  return post<DocumentCheck>('/analyze/document', form);
}
