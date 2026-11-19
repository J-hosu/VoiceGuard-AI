import { StyleSheet, Text, type TextStyle } from 'react-native';
import { useTheme } from '@/core/theme/theme';
import { AppText } from './AppText';

function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * 텍스트 안의 위험 키워드를 하이라이트.
 * (백엔드가 단어 위치를 주지 않아 앱 키워드 사전으로 근사 — 05 항목 4)
 * highlightGrow를 주면 강조 단어만 그만큼(px) 크게 쓴다. 중첩 Text라 웹·네이티브 모두 줄 안에서 커지고,
 * 큰 글씨 모드 배율도 같이 곱한다(AppText가 바깥 글자에 곱하는 배율과 같다).
 */
export function HighlightedText({
  text,
  keywords,
  baseColor,
  highlightColor,
  highlightBg,
  style,
  weightHighlight = '700',
  highlightGrow = 0,
}: {
  text: string;
  keywords: string[];
  baseColor: string;
  highlightColor: string;
  highlightBg?: string;
  style?: TextStyle;
  weightHighlight?: '600' | '700' | '800';
  highlightGrow?: number;
}) {
  const { fontScale } = useTheme();
  const baseSize = (StyleSheet.flatten(style)?.fontSize as number | undefined) ?? 14;
  const grownSize = highlightGrow ? (baseSize + highlightGrow) * fontScale : undefined;
  const valid = keywords.filter((k) => k && k.length > 0).sort((a, b) => b.length - a.length);
  if (valid.length === 0) {
    return (
      <AppText color={baseColor} style={style}>
        {text}
      </AppText>
    );
  }

  const re = new RegExp(`(${valid.map(escapeRegex).join('|')})`, 'g');
  const parts = text.split(re);

  return (
    <AppText color={baseColor} style={style}>
      {parts.map((part, i) => {
        const isHit = valid.some((k) => k === part);
        if (!isHit) return part;
        return (
          <Text
            key={i}
            style={{
              color: highlightColor,
              backgroundColor: highlightBg,
              fontWeight: weightHighlight,
              fontSize: grownSize,
            }}
          >
            {part}
          </Text>
        );
      })}
    </AppText>
  );
}
