/**
 * 문자·문서 검사 결과 카드 (message.tsx, document.tsx 공용).
 * 판정 → 지금 할 일(이렇게 하세요) → 탐지 근거(위험 신호 + 설명) → 화면별 부가 정보(children) 순서로,
 * 판정 아래의 칸은 모두 같은 흰 카드(CheckSection)로 보여준다.
 */
import type { ReactNode } from 'react';
import { Platform, View, type TextStyle } from 'react-native';
import { AppText } from '@/components/AppText';
import { Card } from '@/components/Card';
import { Icon } from '@/components/Icon';
import { useTheme } from '@/core/theme/theme';
import { riskColorFaint, riskColorText, riskIcon, type RiskLevel } from '@/core/utils/riskLevel';
import type { CheckFlag } from '@/data/services/analyzeApi';

// 끊을 곳 없는 긴 글자(링크, 붙여 쓴 기관명)는 칸을 넘칠 때만 아무 데서나 줄을 바꾼다(웹 전용 CSS).
const breakLong: TextStyle | null =
  Platform.OS === 'web' ? ({ overflowWrap: 'anywhere' } as unknown as TextStyle) : null;

const LEVEL_TEXT = { low: '위험 신호가 적어요', medium: '주의가 필요해요', high: '사기 가능성이 높아요' } as const;
// 검사 API의 low/medium/high를 앱 공통 등급(정상/주의/위험)에 맞춰 같은 아이콘·색을 쓴다.
const APP_LEVEL: Record<keyof typeof LEVEL_TEXT, RiskLevel> = { low: 'safe', medium: 'warning', high: 'danger' };

/** 결과 아래쪽의 흰 카드 한 칸(제목 + 내용). 각 화면의 부가 정보(children)도 같은 모양으로 쓴다. */
export function CheckSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Card style={{ gap: 12 }}>
      <AppText weight="800" style={{ fontSize: 16, lineHeight: 22 }}>
        {title}
      </AppText>
      {children}
    </Card>
  );
}

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
  const { colors, isDark } = useTheme();
  const level = APP_LEVEL[riskLevel];
  // 등급색 글자는 옅은 바탕 위에서도 읽히는 글자용 색을 쓴다.
  const tone = riskColorText(level, colors);
  const faint = riskColorFaint(level, colors);
  const stepFill = isDark ? colors.primaryDark : colors.primary;

  return (
    <View style={{ gap: 12 }}>
      {/* 판정: 등급 문구 → 위험도 숫자 → 유형을 세로로 쌓는다(긴 문서 제목도 숫자 옆에서 꺾이지 않게) */}
      <View style={{ backgroundColor: faint, borderRadius: 16, padding: 16, gap: 4 }}>
        <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
          <Icon name={riskIcon(level)} size={20} color={tone} />
          <AppText weight="800" color={tone} style={{ fontSize: 16, lineHeight: 22 }}>
            {LEVEL_TEXT[riskLevel]}
          </AppText>
        </View>
        <View style={{ flexDirection: 'row', alignItems: 'baseline', gap: 8 }}>
          <AppText weight="700" color={colors.textSecondary} style={{ fontSize: 15, lineHeight: 22 }}>
            위험도
          </AppText>
          <AppText weight="800" color={tone} style={{ fontSize: 44, lineHeight: 52 }}>
            {Math.round(riskScore * 100)}%
          </AppText>
        </View>
        <AppText weight="700" style={{ fontSize: 17, lineHeight: 24 }}>
          {title}
        </AppText>
      </View>

      {/* 지금 할 일을 근거보다 먼저 보여준다 */}
      {actions.length > 0 ? (
        <CheckSection title="이렇게 하세요">
          <View style={{ gap: 8 }}>
            {actions.map((action, i) => (
              <View key={i} style={{ flexDirection: 'row', gap: 8 }}>
                <View
                  style={{
                    width: 22,
                    height: 22,
                    borderRadius: 11,
                    marginTop: 1,
                    backgroundColor: stepFill,
                    alignItems: 'center',
                    justifyContent: 'center',
                  }}
                >
                  <AppText weight="800" color="#FFFFFF" style={{ fontSize: 12, lineHeight: 16 }}>
                    {i + 1}
                  </AppText>
                </View>
                <AppText style={[{ fontSize: 15, lineHeight: 24, flex: 1, minWidth: 0 }, breakLong]}>{action}</AppText>
              </View>
            ))}
          </View>
        </CheckSection>
      ) : null}

      {flags.length > 0 || explanation ? (
        <CheckSection title="탐지 근거">
          {flags.length > 0 ? (
            <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8 }}>
              {flags.map((flag) => (
                <View
                  key={flag.code}
                  style={{ paddingHorizontal: 12, paddingVertical: 6, borderRadius: 999, backgroundColor: faint, maxWidth: '100%' }}
                >
                  <AppText weight="700" color={tone} style={[{ fontSize: 13, lineHeight: 18 }, breakLong]}>
                    {flag.label}
                  </AppText>
                </View>
              ))}
            </View>
          ) : null}
          {explanation ? <AppText style={{ fontSize: 15, lineHeight: 24 }}>{explanation}</AppText> : null}
        </CheckSection>
      ) : null}

      {children}
    </View>
  );
}
