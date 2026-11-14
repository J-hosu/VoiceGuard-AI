import { useEffect, useRef, useState } from 'react';
import { ActivityIndicator, Pressable, ScrollView, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { ActionCard, DARK_AGENT_PALETTE, FollowupCard, StageStepper, stageLabel } from '@/components/agentCards';
import { AppText, balanced, keepAll } from '@/components/AppText';
import { HighlightedText } from '@/components/HighlightedText';
import { Icon, type IconName } from '@/components/Icon';
import {
  BlinkDot,
  ChatGlyph,
  GuideGlyph,
  MicGlyph,
  MicOrb,
  PulseOnChange,
  ScoreGauge,
  ShieldGlyph,
  SparkleGlyph,
  StopGlyph,
  rgba,
} from '@/components/realtimeAnalysis';
import { alertFeedback, strongerAlert, type AlertStrength } from '@/core/utils/alertFeedback';
import { formatDuration } from '@/core/utils/format';
import { categorizeType } from '@/core/utils/keywords';
import { type RiskLevel } from '@/core/utils/riskLevel';
import { USE_MOCK_STT } from '@/core/config/env';
import { evidenceBullets } from '@/data/mock/infoContent';
import { getScenario, type Scenario } from '@/data/mock/mockScenarios';
import type { AgentAction } from '@/data/models/types';
import { useCallSession } from '@/hooks/useCallSession';
import { useCallStore } from '@/state/callStore';
import { useDemoStore } from '@/state/demoStore';
import { useSettingsStore } from '@/state/settingsStore';
import { useTranscriptStore } from '@/state/transcriptStore';

/** 실사용(실기기/웹 STT) 라이브 세션: 대본 turns 없이 시작하는 통화. */
const LIVE_SCENARIO: Scenario = {
  id: 'live',
  title: '실시간 통화',
  phone: '실시간 통화',
  source: 'realtime',
  turns: [],
};

// 위험 수준별 색상/문구 (정상=green, 주의=amber, 위험=red). 색만으로 구분하지 않도록 아이콘·문구를 함께 쓴다.
// 등급 이름은 홈·히스토리·결과 화면과 같게(정상/주의/위험), 설명은 짧은 해요체로. 지금 할 일은 AI 대응 카드가 말한다.
type LevelUi = { color: string; text: string; icon: IconName; desc: string };
const LEVEL_UI: Record<RiskLevel, LevelUi> = {
  safe: { color: '#34d399', text: '정상', icon: 'shield-check', desc: '지금까지 의심스러운 말은 없었어요.' },
  warning: {
    color: '#f6a623',
    text: '주의',
    icon: 'alert-triangle',
    desc: '의심스러운 말이 나왔어요.\n개인정보는 알려 주지 마세요.',
  },
  danger: { color: '#ef4444', text: '위험', icon: 'alert-octagon', desc: '보이스피싱일 가능성이 매우 높아요.' },
};
// 서버 연결·마이크 오류로 분석이 멈췄는데 아직 위험 신호가 없던 경우: "정상"으로 보이면 안 된다.
const HALTED_UI: LevelUi = {
  color: '#f6a623',
  text: '분석 중단',
  icon: 'alert-triangle',
  desc: '지금은 위험도를 알 수 없어요.',
};
// 분석 자체가 멈추는 오류만 "분석 중단"으로 보여준다: 서버 연결 실패(wsService)와 마이크·오디오 캡처 실패(audioCapture.web).
// 청크 하나의 전사 실패, 응답 하나의 해석 실패, 후속 답 거부는 서버가 이어서 분석하므로 실시간 표시는 그대로 두고 한 줄 안내만 붙인다.
// useCallSession은 오류를 지우지 않고 마지막 문구만 남기므로 문구 앞부분으로 구분한다(위 파일들의 문구가 바뀌면 함께 바꾼다).
const FATAL_ERROR_PREFIXES = ['서버에 연결할 수 없', '마이크 접근에 실패', '이 브라우저는 오디오 캡처', '오디오 캡처 초기화'];
const isFatalError = (message: string | null) => !!message && FATAL_ERROR_PREFIXES.some((p) => message.startsWith(p));

// 화면 공통 표면 값 (간격 4/8/12/16/24, 카드 모서리 16)
const SCREEN_BG = '#05070d';
const CARD_BG = 'rgba(255,255,255,0.04)';
const CARD_BORDER = 'rgba(255,255,255,0.08)';
const TEXT_TITLE = '#eef2f9';
const TEXT_BODY = '#c9d0dc';
const TEXT_DIM = '#8794a4';
// 화면에는 보이지 않고 화면낭독기만 읽는 요소
const SR_ONLY = { position: 'absolute', width: 1, height: 1, overflow: 'hidden', opacity: 0 } as const;

/** 섹션 카드(공통 테두리/배경) */
function Card({ children }: { children: React.ReactNode }) {
  return (
    <View
      style={{
        borderWidth: 1,
        borderColor: CARD_BORDER,
        borderRadius: 16,
        backgroundColor: CARD_BG,
        padding: 16,
        marginBottom: 12,
      }}
    >
      {children}
    </View>
  );
}

export default function Realtime() {
  const router = useRouter();
  const params = useLocalSearchParams<{ scenario?: string }>();
  // scenario 파라미터가 있으면 데모 시나리오, 없으면 실사용 라이브 세션.
  const scenario: Scenario = params.scenario
    ? getScenario(params.scenario)
    : USE_MOCK_STT
      ? getScenario(null)
      : LIVE_SCENARIO;

  const fetchCalls = useCallStore((s) => s.fetchCalls);
  const saveTranscript = useTranscriptStore((s) => s.save);

  // 종료 마무리(마지막 발화 전사 대기) 상태
  const [finishing, setFinishing] = useState(false);
  const finishingRef = useRef(false);
  // 사용자가 "분석 시작"을 누르기 전까지는 마이크/WS를 열지 않는다.
  const [started, setStarted] = useState(false);

  const vibration = useSettingsStore((s) => s.vibration);
  // 여러 행동이 한꺼번에 도착해도 가장 센 세기로 한 번만 울린다.
  const pendingAlert = useRef<AlertStrength | null>(null);
  const queueAlert = (strength: AlertStrength) => {
    const merged = pendingAlert.current ? strongerAlert(pendingAlert.current, strength) : strength;
    const scheduled = pendingAlert.current !== null;
    pendingAlert.current = merged;
    if (scheduled) return;
    setTimeout(() => {
      if (pendingAlert.current) alertFeedback(pendingAlert.current, vibration);
      pendingAlert.current = null;
    }, 150);
  };

  const { state, start, finish, answerFollowup } = useCallSession(scenario, {
    onAction: (action: AgentAction) => {
      if (finishingRef.current) return;
      // 후속 확인은 화면에 뜨는 시점(due_in_sec 뒤)에 따로 알린다.
      if (action.action_type === 'followup_check') return;
      const danger =
        action.action_type === 'block_guide' ||
        (action.action_type === 'warn' && action.payload.level === 'danger');
      queueAlert(danger ? 'danger' : action.action_type === 'warn' ? 'caution' : 'notice');
    },
    onDangerCross: (s) => {
      if (finishingRef.current) return; // 종료 마무리 중엔 경고 화면으로 튀지 않음
      queueAlert('danger');
      router.push({
        pathname: '/warning',
        params: {
          score: String(s.score),
          patterns: s.matchedPatterns.join('|'),
          category: categorizeType(s.matchedPatterns, s.turns.map((t) => t.content).join(' ')),
        },
      });
    },
  });

  // 발표용 넓은 화면의 분석 패널에 지금 통화를 알려준다.
  const setDemoCall = useDemoStore((s) => s.setCall);
  useEffect(() => {
    if (state.logId > 0) setDemoCall(state.logId, state.running);
  }, [state.logId, state.running, setDemoCall]);

  const beginAnalysis = () => {
    setStarted(true);
    start();
  };

  // 분석이 멈추는 오류(fatal)가 나도 이미 주의·위험이었다면 그 판단은 그대로 보여주고, 정상이던 경우만 "분석 중단"으로 바꾼다.
  // 그 밖의 오류는 분석이 이어지므로 등급·실시간 표시를 그대로 두고 위험도 카드 아래에 한 줄로만 알린다.
  const fatal = isFatalError(state.error);
  const halted = fatal && state.level === 'safe';
  const ui = halted ? HALTED_UI : LEVEL_UI[state.level];
  const score100 = Math.round(state.score * 100);
  const keywords = Array.from(new Set(state.turns.flatMap((t) => t.keywords ?? [])));
  const recentSet = new Set(state.turns.slice(-2).flatMap((t) => t.keywords ?? []));
  const lastTurn = state.turns[state.turns.length - 1];
  const liveText = lastTurn?.content ?? '';
  // 탐지된 패턴이 없으면 빈 목록으로 둬서 "위험 신호 없음" 문구가 나오게 한다.
  // (evidenceBullets는 빈 입력에도 기본 문구를 돌려줘 정상 통화에 의심 표시가 뜨던 문제)
  const bullets = state.matchedPatterns.length > 0 ? evidenceBullets(state.matchedPatterns).slice(0, 3) : [];

  // ── AI 대응(에이전트 행동) ──
  // 후속 확인은 받은 뒤 due_in_sec가 지나야 화면에 띄운다(강한 경고 직후 바로 묻지 않기 위함).
  const followupDueAt = useRef(new Map<number, number>());
  const followupAlerted = useRef(new Set<number>());
  for (const action of state.actions) {
    if (action.action_type === 'followup_check' && !followupDueAt.current.has(action.id)) {
      followupDueAt.current.set(action.id, Date.now() + (action.payload.due_in_sec ?? 0) * 1000);
    }
  }
  // state.elapsedSec가 1초마다 바뀌어 다시 그려지므로 별도 타이머 없이 현재 시각과 비교한다.
  const now = Date.now();
  const dueFollowups = state.actions.filter(
    (a) =>
      a.action_type === 'followup_check' &&
      a.payload.phase === 'in_call' &&
      (a.status === 'answered' || (followupDueAt.current.get(a.id) ?? 0) <= now),
  );
  const activeFollowup = [...dueFollowups].reverse().find((a) => a.status === 'delivered');
  // 최신 행동이 위로 오게 뒤집는다. 경고는 가장 최근 것 하나만 남긴다(주의 → 강한 경고로 바뀌면 교체).
  // 이미 "전화를 끊으세요"라고 안내했다면, 상대에게 되물어 보라는 카드(통화를 이어 가라는 뜻)는 같이 보이지 않는다.
  const latestWarn = [...state.actions].reverse().find((a) => a.action_type === 'warn');
  const hangUp =
    latestWarn?.payload.level === 'danger' || state.actions.some((a) => a.action_type === 'block_guide');
  const actionCards = state.actions
    .filter((a) => a.action_type !== 'followup_check' && (a.action_type !== 'warn' || a.id === latestWarn?.id))
    .filter((a) => !(hangUp && a.action_type === 'verify_guide'))
    .reverse();
  // 피해 긴급 조치(지급정지 등)와 "지금 끊으세요" 경고는 뒤이어 다른 행동이 와도 맨 위(후속 질문보다 위)에 둔다.
  const urgentCards = [
    ...actionCards.filter((a) => a.action_type === 'block_guide'),
    ...actionCards.filter((a) => a.action_type === 'warn' && a.payload.level === 'danger'),
  ];
  const otherCards = actionCards.filter((a) => !urgentCards.includes(a));

  const activeFollowupId = activeFollowup?.id;
  useEffect(() => {
    if (activeFollowupId == null || followupAlerted.current.has(activeFollowupId)) return;
    followupAlerted.current.add(activeFollowupId);
    alertFeedback('notice', vibration);
  }, [activeFollowupId, vibration]);

  // 세션 종료 → (마지막 발화 전사 대기) → 전사 로컬 저장 → 결과 화면.
  const endSession = async () => {
    // 아직 시작 전이면 정리할 세션이 없으므로 바로 나간다.
    if (!started) {
      router.canGoBack() ? router.back() : router.replace('/');
      return;
    }
    if (finishingRef.current) return;
    finishingRef.current = true;
    setFinishing(true);
    const final = await finish(); // 남은 오디오 flush + 마지막 전사 수신 후의 최종 state
    if (final.logId) saveTranscript(final.logId, final.turns);
    void fetchCalls();
    if (final.logId) router.replace(`/result?id=${final.logId}&score=${final.score}`);
    else router.replace('/');
  };

  const hasActions = Boolean(activeFollowup) || actionCards.length > 0;
  const calm = state.level === 'safe' && !fatal;
  // 마이크 오브는 아직 아무 일도 없을 때만 보인다. 위험 신호가 나오면 그 자리를 위험도·대응 카드에 내준다.
  const showOrb = state.level === 'safe' && !hasActions;
  // 오류 안내는 등급 설명을 대신하지 않고 따로 붙인다(주의·위험일 때 "개인정보는 알려 주지 마세요" 같은 안내가 사라지지 않게).
  const errorNotice = state.error ? `연결 오류: ${state.error}` : '';

  return (
    <View style={{ flex: 1, backgroundColor: SCREEN_BG }}>
      <StatusBar style="light" />
      {/* 시작 전에는 위를 덮은 시작 화면만 읽히도록, 뒤의 통화 화면은 보조기기에서 숨기고 키보드 순서에서 뺀다 */}
      <SafeAreaView edges={['top', 'bottom']} style={{ flex: 1 }} aria-hidden={!started}>
        {/* 상단 바: ‹ 종료 · 화면 제목 · 경과 시간 */}
        <View style={{ flexDirection: 'row', alignItems: 'center', height: 52, paddingHorizontal: 8 }}>
          <View style={{ width: 84 }}>
            <Pressable
              onPress={() => endSession()}
              accessibilityRole="button"
              accessibilityLabel="분석 종료"
              focusable={started}
              tabIndex={started ? 0 : -1}
              style={{ width: 44, height: 44, alignItems: 'center', justifyContent: 'center' }}
            >
              <Icon name="chevron-left" size={26} color="#E8ECF2" />
            </Pressable>
          </View>
          <View style={{ flex: 1, flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 8 }}>
            <ShieldGlyph size={18} />
            <AppText weight="700" color={TEXT_TITLE} style={{ fontSize: 16 }}>
              실시간 통화 분석
            </AppText>
          </View>
          <View style={{ width: 84, alignItems: 'flex-end', paddingRight: 8 }}>
            <View
              style={{
                flexDirection: 'row',
                alignItems: 'center',
                gap: 6,
                height: 28,
                paddingHorizontal: 10,
                borderRadius: 14,
                backgroundColor: 'rgba(255,255,255,0.06)',
              }}
            >
              <BlinkDot size={6} color={fatal ? '#f6a623' : '#34d399'} />
              <AppText weight="600" color="#cdd4e0" style={{ fontSize: 13, fontVariant: ['tabular-nums'] }}>
                {formatDuration(state.elapsedSec)}
              </AppText>
            </View>
          </View>
        </View>

        <ScrollView
          style={{ flex: 1 }}
          contentContainerStyle={{ paddingHorizontal: 16, paddingTop: 8, paddingBottom: 24 }}
          showsVerticalScrollIndicator={false}
        >
          {/* 마이크 오브: 듣고 있음을 알린다. 위험해지면 사라져 위험도·대응 카드가 첫 화면에 들어온다 */}
          {showOrb ? (
            <View style={{ marginBottom: 8 }}>
              <MicOrb color={fatal ? TEXT_DIM : ui.color} label={fatal ? '연결 끊김' : '듣는 중'} compact />
            </View>
          ) : null}

          {/* 위험도 카드: 정상일 땐 차분한 중립 카드, 주의·위험이면 등급 색 테두리.
              가장 진한 칸은 아래의 "지금 할 일" 카드에 양보한다. 등급이 바뀌면 화면낭독기가 읽어 준다. */}
          <PulseOnChange trigger={halted ? 'halted' : state.level}>
            {/* 등급이 바뀔 때만 화면낭독기가 읽도록 등급 문구만 담은 숨은 live 영역(점수는 넣지 않는다) */}
            <AppText aria-live="polite" style={SR_ONLY}>
              {`${ui.text}. ${ui.desc}`}
            </AppText>
            <View
              accessible
              accessibilityLabel={`보이스피싱 위험도 ${halted ? '알 수 없음' : `${score100}%`}, ${ui.text}. ${ui.desc}${errorNotice ? `\n${errorNotice}` : ''}`}
              style={{
                borderWidth: 1,
                borderColor: calm ? CARD_BORDER : rgba(ui.color, 0.4),
                borderRadius: 16,
                backgroundColor: calm ? CARD_BG : rgba(ui.color, 0.08),
                padding: 16,
                marginBottom: 12,
              }}
            >
              <View style={{ flexDirection: 'row', alignItems: 'center', gap: 16 }}>
                <ScoreGauge score={halted ? null : score100} color={halted ? TEXT_DIM : ui.color} />
                <View style={{ flex: 1, minWidth: 0 }}>
                  <AppText weight="600" color={TEXT_DIM} style={{ fontSize: 13, lineHeight: 18 }}>
                    보이스피싱 위험도
                  </AppText>
                  <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8, marginTop: 4 }}>
                    <Icon name={ui.icon} size={22} color={ui.color} />
                    <AppText weight="800" style={{ fontSize: 26, lineHeight: 32, color: ui.color }}>
                      {ui.text}
                    </AppText>
                  </View>
                  <AppText color={TEXT_BODY} style={[{ fontSize: 14, lineHeight: 21, marginTop: 4 }, keepAll]}>
                    {ui.desc}
                  </AppText>
                </View>
              </View>
              {/* 오류 한 줄 안내: 분석이 멈춘 오류면 주황 경고 아이콘, 이어서 분석되는 오류면 흐린 안내 아이콘 */}
              {errorNotice ? (
                <View
                  style={{
                    flexDirection: 'row',
                    alignItems: 'flex-start',
                    gap: 8,
                    marginTop: 12,
                    paddingTop: 12,
                    borderTopWidth: 1,
                    borderTopColor: CARD_BORDER,
                  }}
                >
                  <View style={{ marginTop: 1 }}>
                    <Icon name={fatal ? 'alert-triangle' : 'info'} size={16} color={fatal ? '#f6a623' : TEXT_DIM} />
                  </View>
                  <AppText color={TEXT_DIM} numberOfLines={2} style={[{ flex: 1, fontSize: 13, lineHeight: 18 }, keepAll]}>
                    {errorNotice}
                  </AppText>
                </View>
              ) : null}
            </View>
          </PulseOnChange>

          {/* 사기 진행 단계 (상태 추적기) */}
          <Card>
            <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', minHeight: 24, marginBottom: 12 }}>
              <AppText weight="700" color={TEXT_TITLE} style={{ fontSize: 15 }}>
                사기 진행 단계
              </AppText>
              {state.stageIndex > 0 ? (
                <View style={{ backgroundColor: rgba(ui.color, 0.16), paddingHorizontal: 8, paddingVertical: 4, borderRadius: 8 }}>
                  <AppText weight="700" style={{ fontSize: 13, lineHeight: 18, color: ui.color }}>
                    {state.phishingType || stageLabel(state.stage)}
                  </AppText>
                </View>
              ) : null}
            </View>
            <StageStepper
              stageIndex={state.stageIndex}
              predictedNext={state.predictedNext}
              color={ui.color}
              palette={DARK_AGENT_PALETTE}
            />
            <AppText color={state.summary ? TEXT_BODY : TEXT_DIM} style={[{ fontSize: 14, lineHeight: 22, marginTop: 12 }, keepAll]}>
              {state.summary || '사기 단계가 보이면 여기에 알려 드려요.'}
            </AppText>
          </Card>

          {/* AI 대응: 후속 확인 질문 + 에이전트가 결정한 행동. 바깥 카드 없이 행동 카드 자체를 가장 크게 보여준다.
              맨 위 행동 카드(끊으라는 경고, 없으면 가장 최근 행동)가 화면에서 가장 진하다.
              판단 근거 줄은 통화 중에 행동할 수 없는 설명이라 뺀다. */}
          {hasActions ? (
            <View style={{ marginTop: 12, marginBottom: 24 }}>
              {/* 작은 머리말 'AI 대응'(위험도 카드의 '보이스피싱 위험도'와 같은 꼴) 아래에 할 일 제목. 'AI 대응'은 점검 스크립트가 찾는 문구 */}
              <View style={{ marginBottom: 12, paddingHorizontal: 4 }}>
                <View style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}>
                  <SparkleGlyph size={14} color="#8ab4ff" />
                  <AppText weight="700" color="#8ab4ff" style={{ fontSize: 13, lineHeight: 18 }}>
                    AI 대응
                  </AppText>
                </View>
                <AppText weight="800" color="#f6f8fc" style={{ fontSize: 18, lineHeight: 24, marginTop: 4 }}>
                  지금 이렇게 하세요
                </AppText>
              </View>
              <View style={{ gap: 8 }}>
                {urgentCards.map((action, i) => (
                  <ActionCard
                    key={action.id}
                    action={action}
                    palette={DARK_AGENT_PALETTE}
                    featured={i === 0}
                    showReason={false}
                  />
                ))}
                {activeFollowup ? (
                  <FollowupCard
                    action={activeFollowup}
                    palette={DARK_AGENT_PALETTE}
                    onAnswer={(answer) => answerFollowup(activeFollowup.id, answer)}
                  />
                ) : null}
                {otherCards.map((action, i) => (
                  <ActionCard
                    key={action.id}
                    action={action}
                    palette={DARK_AGENT_PALETTE}
                    featured={urgentCards.length === 0 && i === 0}
                    showReason={false}
                  />
                ))}
              </View>
            </View>
          ) : null}

          {/* 방금 들린 말 + 주요 단어 */}
          <Card>
            <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
              <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
                <ChatGlyph size={18} color={TEXT_DIM} />
                <AppText weight="700" color={TEXT_TITLE} style={{ fontSize: 15 }}>
                  방금 들린 말
                </AppText>
              </View>
              {!fatal ? (
                <View style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}>
                  <BlinkDot size={6} color="#ef4444" period={1300} />
                  <AppText weight="700" color="#f87171" style={{ fontSize: 12, lineHeight: 16 }}>
                    실시간
                  </AppText>
                </View>
              ) : null}
            </View>
            {liveText ? (
              <HighlightedText
                text={liveText}
                keywords={lastTurn?.keywords ?? []}
                baseColor="#dfe4ee"
                highlightColor="#f6a623"
                weightHighlight="700"
                style={{ fontSize: 15, lineHeight: 24, ...keepAll }}
              />
            ) : (
              <AppText color={TEXT_DIM} style={{ fontSize: 14, lineHeight: 22 }}>
                {fatal ? '분석이 멈춰 말소리를 받지 못하고 있어요.' : '말소리를 듣고 있어요…'}
              </AppText>
            )}
            {keywords.length > 0 ? (
              <View style={{ marginTop: 16, paddingTop: 12, borderTopWidth: 1, borderTopColor: CARD_BORDER }}>
                <AppText weight="600" color={TEXT_DIM} style={{ fontSize: 13, lineHeight: 18, marginBottom: 8 }}>
                  주요 단어
                </AppText>
                <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8 }}>
                  {keywords.slice(0, 8).map((k) => (
                    <View
                      key={k}
                      style={{
                        flexDirection: 'row',
                        alignItems: 'center',
                        gap: 6,
                        paddingHorizontal: 12,
                        height: 32,
                        borderRadius: 8,
                        backgroundColor: 'rgba(255,255,255,0.07)',
                      }}
                    >
                      <AppText weight="600" color="#dfe4ee" style={{ fontSize: 13 }}>
                        {k}
                      </AppText>
                      {recentSet.has(k) ? <View style={{ width: 6, height: 6, borderRadius: 3, backgroundColor: '#f6a623' }} /> : null}
                    </View>
                  ))}
                </View>
              </View>
            ) : null}
          </Card>

          {/* 탐지 근거 (결과 화면 탭과 같은 이름) */}
          <Card>
            <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8, marginBottom: 12 }}>
              <SparkleGlyph size={18} color="#8ab4ff" />
              <AppText weight="700" color={TEXT_TITLE} style={{ fontSize: 15 }}>
                탐지 근거
              </AppText>
            </View>
            <View style={{ gap: 8 }}>
              {bullets.length > 0 ? (
                bullets.map((b, i) => (
                  <View key={i} style={{ flexDirection: 'row', gap: 8, alignItems: 'flex-start' }}>
                    <View style={{ marginTop: 7, width: 6, height: 6, borderRadius: 3, backgroundColor: ui.color }} />
                    <AppText color={TEXT_BODY} style={[{ fontSize: 14, lineHeight: 20, flex: 1 }, keepAll]}>
                      {b}
                    </AppText>
                  </View>
                ))
              ) : (
                <View style={{ flexDirection: 'row', gap: 8, alignItems: 'flex-start' }}>
                  <View
                    style={{
                      marginTop: 7,
                      width: 6,
                      height: 6,
                      borderRadius: 3,
                      backgroundColor: fatal ? '#f6a623' : state.level !== 'safe' ? ui.color : '#34d399',
                    }}
                  />
                  <AppText color={TEXT_BODY} style={[{ fontSize: 14, lineHeight: 20, flex: 1 }, keepAll]}>
                    {fatal
                      ? '분석이 멈춰 위험 신호를 확인할 수 없어요.'
                      : state.level !== 'safe'
                        ? '근거를 정리하고 있어요.'
                        : '아직 보이스피싱 신호는 없어요.'}
                  </AppText>
                </View>
              )}
            </View>
          </Card>
        </ScrollView>

        {/* 하단 고정 액션: 분석 종료가 이 화면의 주 버튼. 통화 중 실수로 보호를 끄지 않도록 분석을 끝내는 버튼은
            하나만 둔다(대화 기록은 분석 종료 뒤 결과 화면의 '대화 내용' 탭에서 본다). */}
        <View
          style={{
            flexDirection: 'row',
            gap: 8,
            paddingHorizontal: 16,
            paddingVertical: 12,
            borderTopWidth: 1,
            borderTopColor: CARD_BORDER,
            backgroundColor: SCREEN_BG,
          }}
        >
          <ActionButton onPress={() => router.push('/prevention')} focusable={started}>
            <GuideGlyph size={18} color="#dfe4ee" />
            <AppText weight="600" color="#dfe4ee" style={{ fontSize: 15, textAlign: 'center', flexShrink: 1 }}>
              대처 방법
            </AppText>
          </ActionButton>
          <ActionButton primary onPress={() => endSession()} focusable={started}>
            <StopGlyph size={18} color="#FFFFFF" />
            <AppText weight="800" color="#FFFFFF" style={{ fontSize: 15, textAlign: 'center', flexShrink: 1 }}>
              분석 종료
            </AppText>
          </ActionButton>
        </View>
      </SafeAreaView>

      {/* 시작 대기 화면: "분석 시작"을 누르기 전까지 마이크/분석을 열지 않는다 */}
      {!started ? (
        <View
          style={{
            position: 'absolute',
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            backgroundColor: SCREEN_BG,
          }}
        >
          <SafeAreaView edges={['top', 'bottom']} style={{ flex: 1 }}>
            <View style={{ flexDirection: 'row', alignItems: 'center', height: 52, paddingHorizontal: 8 }}>
              <Pressable
                onPress={() => endSession()}
                accessibilityRole="button"
                accessibilityLabel="뒤로"
                style={{ width: 44, height: 44, alignItems: 'center', justifyContent: 'center' }}
              >
                <Icon name="chevron-left" size={26} color="#E8ECF2" />
              </Pressable>
            </View>
            <View style={{ flex: 1, alignItems: 'center', justifyContent: 'center', paddingHorizontal: 24, paddingBottom: 52 }}>
              <View
                style={{
                  width: 96,
                  height: 96,
                  borderRadius: 48,
                  alignItems: 'center',
                  justifyContent: 'center',
                  backgroundColor: 'rgba(91,155,255,0.1)',
                }}
              >
                <ShieldGlyph size={44} />
              </View>
              <AppText weight="800" color="#f6f8fc" style={{ fontSize: 24, lineHeight: 32, marginTop: 24 }}>
                실시간 통화 분석
              </AppText>
              <AppText color="#a8b0be" style={[{ fontSize: 14, textAlign: 'center', lineHeight: 22, marginTop: 8 }, balanced]}>
                버튼을 누르면 통화를 들으며{'\n'}보이스피싱 위험을 바로 알려 드려요
              </AppText>
              <Pressable
                onPress={beginAnalysis}
                accessibilityRole="button"
                accessibilityLabel="분석 시작"
                style={({ pressed }) => ({
                  marginTop: 32,
                  width: '100%',
                  maxWidth: 320,
                  height: 56,
                  borderRadius: 16,
                  flexDirection: 'row',
                  alignItems: 'center',
                  justifyContent: 'center',
                  gap: 8,
                  backgroundColor: '#2563EB',
                  opacity: pressed ? 0.85 : 1,
                })}
              >
                <MicGlyph size={20} color="#FFFFFF" />
                <AppText weight="800" color="#FFFFFF" style={{ fontSize: 17 }}>
                  분석 시작
                </AppText>
              </Pressable>
            </View>
          </SafeAreaView>
        </View>
      ) : null}

      {/* 종료 마무리 오버레이: 마지막 발화 전사를 받는 동안 표시 */}
      {finishing ? (
        <View
          style={{
            position: 'absolute',
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            backgroundColor: 'rgba(5,7,13,0.88)',
            alignItems: 'center',
            justifyContent: 'center',
            gap: 12,
          }}
        >
          <ActivityIndicator size="large" color="#5b9bff" />
          <AppText weight="700" color={TEXT_TITLE} style={{ fontSize: 16 }}>
            분석 마무리 중…
          </AppText>
          <AppText color={TEXT_DIM} style={{ fontSize: 13 }}>
            마지막 대화까지 저장하고 있어요
          </AppText>
        </View>
      ) : null}
    </View>
  );
}

function ActionButton({
  children,
  onPress,
  primary = false,
  focusable = true,
}: {
  children: React.ReactNode;
  onPress: () => void;
  primary?: boolean;
  focusable?: boolean;
}) {
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      focusable={focusable}
      tabIndex={focusable ? 0 : -1}
      style={({ pressed }) => ({
        flex: primary ? 1.5 : 1,
        flexDirection: 'row',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 6,
        minHeight: 48,
        paddingVertical: 6,
        paddingHorizontal: 8,
        borderRadius: 12,
        backgroundColor: primary ? '#2563EB' : 'rgba(255,255,255,0.07)',
        opacity: pressed ? 0.85 : 1,
      })}
    >
      {children}
    </Pressable>
  );
}
