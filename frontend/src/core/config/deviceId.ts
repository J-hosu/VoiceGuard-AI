import AsyncStorage from '@react-native-async-storage/async-storage';
import { Platform } from 'react-native';

/**
 * 기기(브라우저)마다 다른 번호.
 *
 * QR로 여러 사람이 같은 서버에 접속하므로, 통화 기록·홈 통계가 섞이지 않게 기기마다 번호를 하나 만들어
 * 저장해 두고 실시간 분석 시작(start), 통화 목록 조회, 녹음 파일 업로드에 함께 보낸다. 서버는 이미
 * device_id로 통화를 저장·조회한다. 로그인이 아니라 구분용 번호라 다른 기기에서 같은 기록을 이어 볼 수는 없다.
 *
 * 웹은 localStorage에 동기로 읽고 쓴다. 네이티브는 앱 시작 때 loadDeviceId()로 AsyncStorage에서 불러온다.
 */
const KEY = 'voiceguard.device_id';
const MIN_ID = 100_000_000; // 예전 고정값(1)이나 직접 넣은 작은 번호와 겹치지 않게 9~10자리만 쓴다
let cached: number | null = null;

function makeId(): number {
  return MIN_ID + Math.floor(Math.random() * 2_000_000_000); // 2^31 아래 (SQLite INTEGER, JS 안전 정수)
}

function parse(value: string | null): number | null {
  const n = Number(value);
  return Number.isInteger(n) && n >= MIN_ID ? n : null;
}

export function getDeviceId(): number {
  if (cached !== null) return cached;
  if (Platform.OS === 'web' && typeof window !== 'undefined') {
    try {
      cached = parse(window.localStorage.getItem(KEY));
      if (cached === null) {
        cached = makeId();
        window.localStorage.setItem(KEY, String(cached));
      }
      return cached;
    } catch {
      // 저장소가 막힌 브라우저(사생활 보호 모드 등): 이 탭에서만 쓰는 번호
    }
  }
  cached = makeId();
  if (Platform.OS !== 'web') void AsyncStorage.setItem(KEY, String(cached)).catch(() => undefined);
  return cached;
}

/** 네이티브 앱 시작 때 저장된 번호를 불러온다. 웹은 getDeviceId()가 바로 읽으므로 부를 필요가 없다. */
export async function loadDeviceId(): Promise<number> {
  if (Platform.OS !== 'web' && cached === null) {
    try {
      cached = parse(await AsyncStorage.getItem(KEY));
    } catch {
      cached = null;
    }
  }
  return getDeviceId();
}
