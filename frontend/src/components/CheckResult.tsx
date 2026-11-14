/**
 * 문자·문서 검사 결과 카드 (message.tsx, document.tsx 공용).
 * 위험도, 유형, 위험 신호, 설명, 권장 행동을 같은 모양으로 보여준다.
 */
import type { ReactNode } from 'react';
import { View } from 'react-native';
import { AppText } from '@/components/AppText';
import { Icon } from '@/components/Icon';
import { useTheme } from '@/core/theme/theme';
import type { CheckFlag } from '@/data/services/analyzeApi';

const LEVEL_TEXT = { low: '위험 신호가 적어요', medium: '주의가 필요해요', high: '사기 가능성이 높아요' } as const;

export function CheckResult({
  riskScore,
  riskLevel,
  title,
  flags,
  explanation,
  actions,
  children,
}: {
  riskScore: number;
  riskLevel: 'low' | 'medium' | 'high';
  title: string;
  flags: CheckFlag[];
  explanation: string;
  actions: string[];
  children?: ReactNode;
}) {
  const { colors } = useTheme();
  const tone = riskLevel === 'high' ? colors.danger : riskLevel === 'medium' ? colors.warning : colors.safe;
  const faint = riskLevel === 'high' ? colors.dangerFaint : riskLevel === 'medium' ? colors.warningFaint : colors.safeFaint;

  return (
    <View style={{ gap: 14 }}>
      <View style={{ backgroundColor: faint, borderRadius: 18, padding: 18, gap: 6 }}>
        <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
          <Icon name={riskLevel === 'low' ? 'check-circle' : 'shield-check'} size={18} color={tone} />
          <AppText weight="800" color={tone} style={{ fontSize: 15 }}>
            {LEVEL_TEXT[riskLevel]}
          </AppText>
        </View>
        <View style={{ flexDirection: 'row', alignItems: 'baseline', gap: 10 }}>
          <AppText weight="800" color={tone} style={{ fontSize: 40, lineHeight: 46 }}>
            {Math.round(riskScore * 100)}%
          </AppText>
          <AppText weight="700" color={colors.text} style={{ fontSize: 15, flex: 1 }}>
            {title}
          </AppText>
        </View>
      </View>

      {flags.length > 0 ? (
        <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8 }}>
          {flags.map((flag) => (
            <View
              key={flag.code}
              style={{ paddingHorizontal: 11, paddingVertical: 6, borderRadius: 999, backgroundColor: colors.dangerFaint }}
            >
              <AppText weight="700" color={colors.dangerText} style={{ fontSize: 12.5 }}>
                {flag.label}
              </AppText>
            </View>
          ))}
        </View>
      ) : null}

      {explanation ? (
        <AppText color={colors.textSecondary} style={{ fontSize: 14.5, lineHeight: 23 }}>
          {explanation}
        </AppText>
      ) : null}

      {children}

      {actions.length > 0 ? (
        <View style={{ borderWidth: 1, borderColor: colors.border, borderRadius: 16, padding: 16, gap: 10 }}>
          <AppText weight="800" style={{ fontSize: 15 }}>
            이렇게 하세요
          </AppText>
          {actions.map((action, i) => (
            <View key={i} style={{ flexDirection: 'row', gap: 10 }}>
              <AppText weight="800" color={colors.primary} style={{ fontSize: 13.5, width: 16 }}>
                {i + 1}
              </AppText>
              <AppText color={colors.text} style={{ fontSize: 14, lineHeight: 21, flex: 1 }}>
                {action}
              </AppText>
            </View>
          ))}
        </View>
      ) : null}
    </View>
  );
}
