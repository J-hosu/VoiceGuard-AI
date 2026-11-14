import { Linking, Pressable, View } from 'react-native';
import { AppText } from '@/components/AppText';
import { Icon, type IconName } from '@/components/Icon';
import { BASE_URL_HTTP } from '@/core/config/env';
import type { ThemeColors } from '@/core/theme/colors';
import type { AgentAction, ScamStage } from '@/data/models/types';

/**
 * 능동형 대응 루프 UI 조각 (실시간 화면·결과 화면 공용).
 *  - StageStepper: 사기 진행 단계 표시
 *  - ActionCard: 에이전트가 결정한 행동 1건
 *  - FollowupCard: 후속 확인 질문
 *
 * 실시간 화면은 고정 다크 팔레트, 결과 화면은 테마 색을 쓰므로 색은 palette로 받는다.
 */
export interface AgentPalette {
  text: string;
  sub: string;
  muted: string;
  cardBg: string;
  border: string;
  accent: string;
  danger: string;
  warning: string;
  safe: string;
}

export const DARK_AGENT_PALETTE: AgentPalette = {
  text: '#eef2f9',
  sub: '#c3cad6',
  muted: '#8794a4',
  cardBg: 'rgba(255,255,255,0.04)',
  border: 'rgba(255,255,255,0.09)',
  accent: '#8ab4ff',
  danger: '#ef4444',
  warning: '#f6a623',
  safe: '#34d399',
};

export function paletteFromTheme(colors: ThemeColors): AgentPalette {
  return {
    text: colors.text,
    sub: colors.textSecondary,
    muted: colors.textMuted,
    cardBg: colors.cardAlt,
    border: colors.border,
    accent: colors.primary,
    danger: colors.danger,
    warning: colors.warning,
    safe: colors.safe,
  };
}

/** stage_index 1~6 에 대응하는 짧은 라벨 */
export const STAGE_STEPS: { stage: ScamStage; label: string }[] = [
  { stage: 'approach', label: '접근' },
  { stage: 'impersonation', label: '사칭' },
  { stage: 'pressure', label: '위기 조성' },
  { stage: 'isolation', label: '고립' },
  { stage: 'extraction', label: '정보 요구' },
  { stage: 'payment', label: '금전 요구' },
];

export function stageLabel(stage: string | null | undefined): string {
  return STAGE_STEPS.find((s) => s.stage === stage)?.label ?? '';
}

/** 수법 태그 코드 → 화면 라벨. safe=true는 정상 신호. (API_SPEC_V2 2.3) */
export const TACTIC_LABELS: Record<string, { label: string; safe?: boolean }> = {
  authority_claim: { label: '기관·직함 사칭' },
  crime_involvement: { label: '범죄 연루 통보' },
  legal_threat: { label: '구속·처벌 위협' },
  urgency: { label: '시간 압박' },
  secrecy: { label: '비밀 유지 강요' },
  isolation: { label: '통화 유지·이동 지시' },
  info_request: { label: '개인정보 요구' },
  app_install: { label: '앱 설치 유도' },
  link_click: { label: '링크 접속 유도' },
  money_transfer: { label: '이체·송금 요구' },
  cash_withdrawal: { label: '현금 인출 요구' },
  loan_bait: { label: '저금리·선상환 미끼' },
  official_channel: { label: '공식 창구 안내', safe: true },
  no_pressure: { label: '강요 없음', safe: true },
};

/** 반투명 색 만들기 (#rrggbb 전용) */
function withAlpha(hex: string, alpha: number): string {
  const m = /^#([0-9a-f]{6})$/i.exec(hex);
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

/**
 * 사기 진행 단계 표시. 도달한 단계까지 색을 채우고, 다음 예상 단계는 점선 테두리로 예고한다.
 * 수법에 따라 단계를 건너뛰기도 하므로 "지금까지 도달한 가장 높은 단계"를 기준으로 그린다.
 */
export function StageStepper({
  stageIndex,
  predictedNext,
  color,
  palette,
}: {
  stageIndex: number;
  predictedNext?: string | null;
  color: string;
  palette: AgentPalette;
}) {
  const predictedIndex = predictedNext ? STAGE_STEPS.findIndex((s) => s.stage === predictedNext) + 1 : 0;

  return (
    <View style={{ flexDirection: 'row', alignItems: 'flex-start' }}>
      {STAGE_STEPS.map((step, i) => {
        const index = i + 1;
        const reached = index <= stageIndex;
        const current = index === stageIndex;
        const predicted = !reached && index === predictedIndex;
        return (
          <View key={step.stage} style={{ flex: 1, alignItems: 'center' }}>
            <View style={{ flexDirection: 'row', alignItems: 'center', width: '100%' }}>
              {/* 왼쪽 연결선 */}
              <View
                style={{
                  flex: 1,
                  height: 2,
                  backgroundColor: i === 0 ? 'transparent' : reached ? color : palette.border,
                }}
              />
              <View
                style={{
                  width: current ? 22 : 16,
                  height: current ? 22 : 16,
                  borderRadius: 11,
                  alignItems: 'center',
                  justifyContent: 'center',
                  backgroundColor: reached ? color : 'transparent',
                  borderWidth: reached ? 0 : 1.5,
                  borderStyle: predicted ? 'dashed' : 'solid',
                  borderColor: predicted ? color : palette.border,
                }}
              >
                {current ? <View style={{ width: 8, height: 8, borderRadius: 4, backgroundColor: '#FFFFFF' }} /> : null}
              </View>
              {/* 오른쪽 연결선 */}
              <View
                style={{
                  flex: 1,
                  height: 2,
                  backgroundColor:
                    i === STAGE_STEPS.length - 1 ? 'transparent' : index < stageIndex ? color : palette.border,
                }}
              />
            </View>
            <AppText
              weight={current ? '800' : reached ? '600' : '500'}
              color={current ? color : reached ? palette.sub : palette.muted}
              style={{ fontSize: 10.5, marginTop: 6, textAlign: 'center' }}
              numberOfLines={1}
            >
              {step.label}
            </AppText>
            {predicted ? (
              <AppText weight="700" color={color} style={{ fontSize: 9.5, marginTop: 1 }}>
                다음 예상
              </AppText>
            ) : null}
          </View>
        );
      })}
    </View>
  );
}

const ACTION_ICONS: Record<string, IconName> = {
  warn: 'alert-triangle',
  verify_guide: 'user',
  official_number: 'phone-forwarded',
  guardian_alert: 'bell',
  block_guide: 'lock',
  evidence_preserve: 'record',
  report_guide: 'file',
  followup_check: 'info',
};

function actionColor(action: AgentAction, palette: AgentPalette): string {
  if (action.action_type === 'warn') return action.payload.level === 'danger' ? palette.danger : palette.warning;
  if (action.action_type === 'block_guide') return palette.danger;
  return palette.accent;
}

/** 숫자로만 된 번호만 전화 걸기 링크로 만든다("카드 뒷면의 대표번호" 같은 안내 문구는 제외). */
function isDialable(phone: string): boolean {
  return /^[0-9-]+$/.test(phone);
}

function PhonePill({ label, phone, color }: { label?: string; phone: string; color: string }) {
  const dialable = isDialable(phone);
  const body = (
    <View
      style={{
        flexDirection: 'row',
        alignItems: 'center',
        gap: 7,
        alignSelf: 'flex-start',
        paddingHorizontal: 12,
        paddingVertical: 8,
        borderRadius: 10,
        backgroundColor: withAlpha(color, 0.14),
        borderWidth: 1,
        borderColor: withAlpha(color, 0.45),
      }}
    >
      <Icon name="phone" size={15} color={color} />
      <AppText weight="800" color={color} style={{ fontSize: 14 }}>
        {label ? `${label} ${phone}` : phone}
      </AppText>
    </View>
  );
  if (!dialable) return body;
  return (
    <Pressable onPress={() => void Linking.openURL(`tel:${phone}`)} accessibilityLabel={`${label ?? ''} ${phone} 전화 걸기`}>
      {body}
    </Pressable>
  );
}

/** 에이전트 행동 카드. 제목·안내문·행동별 내용(번호, 질문, 절차)·결정 이유를 보여준다. */
export function ActionCard({ action, palette }: { action: AgentAction; palette: AgentPalette }) {
  const color = actionColor(action, palette);
  const { payload } = action;

  return (
    <View
      style={{
        borderRadius: 14,
        borderWidth: 1,
        borderColor: withAlpha(color, 0.45),
        backgroundColor: withAlpha(color, 0.07),
        padding: 13,
        gap: 8,
      }}
    >
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
        <Icon name={ACTION_ICONS[action.action_type] ?? 'info'} size={17} color={color} />
        <AppText weight="800" color={color} style={{ fontSize: 14.5, flex: 1 }}>
          {action.title}
        </AppText>
      </View>
      <AppText color={palette.sub} style={{ fontSize: 13, lineHeight: 20 }}>
        {action.message}
      </AppText>

      {action.action_type === 'official_number' && payload.phone ? (
        <PhonePill label={payload.org} phone={payload.phone} color={color} />
      ) : null}

      {payload.questions?.length ? (
        <View style={{ gap: 6 }}>
          {payload.questions.map((q, i) => (
            <View key={i} style={{ flexDirection: 'row', gap: 7 }}>
              <AppText weight="800" color={color} style={{ fontSize: 13 }}>
                Q{i + 1}
              </AppText>
              <AppText color={palette.text} style={{ fontSize: 13, lineHeight: 20, flex: 1 }}>
                {q}
              </AppText>
            </View>
          ))}
        </View>
      ) : null}

      {payload.steps?.length ? (
        <View style={{ gap: 7 }}>
          {payload.steps.map((step, i) => (
            <View key={i} style={{ flexDirection: 'row', gap: 8 }}>
              <View
                style={{
                  width: 19,
                  height: 19,
                  borderRadius: 10,
                  backgroundColor: color,
                  alignItems: 'center',
                  justifyContent: 'center',
                  marginTop: 1,
                }}
              >
                <AppText weight="800" color="#FFFFFF" style={{ fontSize: 11 }}>
                  {i + 1}
                </AppText>
              </View>
              <AppText color={palette.text} style={{ fontSize: 13, lineHeight: 20, flex: 1 }}>
                {step}
              </AppText>
            </View>
          ))}
        </View>
      ) : null}

      {payload.phones?.length ? (
        <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8 }}>
          {payload.phones.map((phone) => (
            <PhonePill key={phone} phone={phone} color={color} />
          ))}
        </View>
      ) : null}

      {action.action_type === 'report_guide' && payload.report_url ? (
        <Pressable
          onPress={() => void Linking.openURL(`${BASE_URL_HTTP}${payload.report_url}?format=md`)}
          accessibilityLabel="신고용 요약 열기"
          style={{
            alignSelf: 'flex-start',
            paddingHorizontal: 13,
            paddingVertical: 8,
            borderRadius: 10,
            backgroundColor: withAlpha(color, 0.14),
            borderWidth: 1,
            borderColor: withAlpha(color, 0.45),
          }}
        >
          <AppText weight="800" color={color} style={{ fontSize: 13.5 }}>
            신고용 요약 열기
          </AppText>
        </Pressable>
      ) : null}

      {action.reason ? (
        <AppText color={palette.muted} style={{ fontSize: 11.5, lineHeight: 17 }}>
          AI 판단 근거 · {action.reason}
        </AppText>
      ) : null}
    </View>
  );
}

/** 후속 확인 질문 카드. 답을 고르면 onAnswer가 호출된다. 이미 답했으면 고른 답만 보여준다. */
export function FollowupCard({
  action,
  palette,
  onAnswer,
}: {
  action: AgentAction;
  palette: AgentPalette;
  onAnswer: (answer: string) => void;
}) {
  const options = action.payload.options ?? [];
  const answered = action.status === 'answered';
  const chosen = options.find((o) => o.answer === action.response);

  return (
    <View
      style={{
        borderRadius: 14,
        borderWidth: 1.5,
        borderColor: withAlpha(palette.accent, 0.6),
        backgroundColor: withAlpha(palette.accent, 0.1),
        padding: 14,
        gap: 10,
      }}
    >
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
        <Icon name="info" size={17} color={palette.accent} />
        <AppText weight="800" color={palette.text} style={{ fontSize: 15, flex: 1 }}>
          {action.payload.question ?? action.title}
        </AppText>
      </View>
      {answered ? (
        <AppText weight="600" color={palette.sub} style={{ fontSize: 13 }}>
          답변: {chosen?.label ?? action.response}
        </AppText>
      ) : (
        <View style={{ gap: 8 }}>
          {options.map((option) => (
            <Pressable
              key={option.answer}
              onPress={() => onAnswer(option.answer)}
              accessibilityLabel={option.label}
              style={{
                paddingVertical: 11,
                paddingHorizontal: 14,
                borderRadius: 11,
                borderWidth: 1,
                borderColor: palette.border,
                backgroundColor: palette.cardBg,
              }}
            >
              <AppText weight="700" color={palette.text} style={{ fontSize: 13.5 }}>
                {option.label}
              </AppText>
            </Pressable>
          ))}
        </View>
      )}
    </View>
  );
}
