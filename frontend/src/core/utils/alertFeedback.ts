import { Platform, Vibration } from 'react-native';
import * as Haptics from 'expo-haptics';

/** 알림 세기. notice=가벼운 안내, caution=주의 경고, danger=강한 경고·긴급 조치 */
export type AlertStrength = 'notice' | 'caution' | 'danger';

const STRENGTH_ORDER: Record<AlertStrength, number> = { notice: 0, caution: 1, danger: 2 };

// React Native 패턴: [대기, 진동, 대기, 진동 ...] (ms). iOS는 길이를 무시하고 횟수만 반영한다.
const PATTERNS: Record<AlertStrength, number[]> = {
  notice: [0, 120],
  caution: [0, 250, 150, 250],
  danger: [0, 500, 200, 500, 200, 500],
};

/** 더 센 쪽을 고른다(여러 알림이 한꺼번에 올 때 한 번만 울리기 위함). */
export function strongerAlert(a: AlertStrength, b: AlertStrength): AlertStrength {
  return STRENGTH_ORDER[a] >= STRENGTH_ORDER[b] ? a : b;
}

/**
 * 탐지·대응 알림을 진동으로 전달한다.
 *
 * - 앱(iOS/Android): 진동 + 햅틱.
 * - 웹: 진동 API가 있는 브라우저(주로 Android Chrome)에서만 울린다. iOS Safari와 데스크톱
 *   브라우저에는 진동 API가 없어 조용히 넘어간다.
 */
export function alertFeedback(strength: AlertStrength, enabled = true): void {
  if (!enabled) return;
  const pattern = PATTERNS[strength];

  if (Platform.OS === 'web') {
    const nav = typeof navigator !== 'undefined' ? navigator : undefined;
    if (nav && typeof nav.vibrate === 'function') {
      try {
        // 웹 패턴은 [진동, 대기, 진동 ...] 이라 맨 앞 대기값을 뺀다.
        nav.vibrate(pattern.slice(1));
      } catch {
        // 사용자 제스처 전이거나 권한 정책으로 막힌 경우 무시
      }
    }
    return;
  }

  Vibration.vibrate(pattern);
  const type =
    strength === 'danger'
      ? Haptics.NotificationFeedbackType.Error
      : strength === 'caution'
        ? Haptics.NotificationFeedbackType.Warning
        : Haptics.NotificationFeedbackType.Success;
  void Haptics.notificationAsync(type).catch(() => {});
}
