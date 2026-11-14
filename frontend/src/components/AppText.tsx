import { Platform, Text, type TextProps, type TextStyle, StyleSheet } from 'react-native';
import { useTheme } from '@/core/theme/theme';

type Weight = '400' | '500' | '600' | '700' | '800';

/**
 * 한글이 단어 중간에서 줄바꿈되지 않게 하는 스타일(웹 전용 CSS).
 * AppText에는 기본으로 들어간다. TextInput처럼 AppText가 아닌 곳에는 style 배열로 덧붙여 쓴다.
 * text-wrap-style: pretty는 마지막 줄에 한 단어만 남는 것을 줄인다. 축약형 text-wrap은 쓰지 않는다:
 * numberOfLines={1}의 white-space: nowrap(줄바꿈 금지)까지 덮어써 한 줄 말줄임이 풀린다.
 * 네이티브는 기본 줄바꿈을 그대로 쓴다.
 */
export const keepAll: TextStyle | null =
  Platform.OS === 'web' ? ({ wordBreak: 'keep-all', textWrapStyle: 'pretty' } as unknown as TextStyle) : null;

/** 가운데 정렬한 여러 줄 제목·안내문용: 줄 길이를 고르게 나눈다(웹 전용). */
export const balanced: TextStyle | null =
  Platform.OS === 'web' ? ({ wordBreak: 'keep-all', textWrapStyle: 'balance' } as unknown as TextStyle) : null;

/**
 * 앱 공통 텍스트.
 * - 큰 글씨 모드(설정) 배율을 fontSize에 자동 적용. 지정된 줄 높이·자간도 같은 배율로 키운다.
 * - weight로 굵기 지정, 기본 색은 테마 본문색.
 */
export function AppText({
  weight = '400',
  color,
  style,
  children,
  ...rest
}: TextProps & { weight?: Weight; color?: string }) {
  const { colors, fontScale } = useTheme();
  const flat = (StyleSheet.flatten(style) ?? {}) as TextStyle;
  const baseSize = typeof flat.fontSize === 'number' ? flat.fontSize : 14;
  const scaled: TextStyle = { fontSize: baseSize * fontScale };
  if (fontScale !== 1) {
    if (typeof flat.lineHeight === 'number') scaled.lineHeight = Math.round(flat.lineHeight * fontScale);
    if (typeof flat.letterSpacing === 'number') scaled.letterSpacing = flat.letterSpacing * fontScale;
  }

  return (
    <Text {...rest} style={[{ color: color ?? colors.text, fontWeight: weight }, keepAll, style, scaled]}>
      {children}
    </Text>
  );
}
