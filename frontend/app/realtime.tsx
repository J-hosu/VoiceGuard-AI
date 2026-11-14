import { useEffect, useRef, useState } from 'react';
import { ActivityIndicator, Pressable, ScrollView, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { ActionCard, DARK_AGENT_PALETTE, FollowupCard, StageStepper, stageLabel } from '@/components/agentCards';
import { AppText } from '@/components/AppText';
import { HighlightedText } from '@/components/HighlightedText';
import { Icon } from '@/components/Icon';
import {
  AiNode,
  BlinkDot,
  ChatGlyph,
  FileGlyph,
  GuideGlyph,
  MicOrb,
  ScoreGauge,
  ShieldGlyph,
  SparkleGlyph,
  StopGlyph,
  rgba,
} from '@/components/realtimeAnalysis';
import { alertFeedback, strongerAlert, type AlertStrength } from '@/core/utils/alertFeedback';
import { categorizeType } from '@/core/utils/keywords';
import { type RiskLevel } from '@/core/utils/riskLevel';
import { USE_MOCK_STT } from '@/core/config/env';
import { evidenceBullets } from '@/data/mock/infoContent';
import { getScenario, type Scenario } from '@/data/mock/mockScenarios';
import type { AgentAction } from '@/data/models/types';
import { useCallSession } from '@/hooks/useCallSession';
import { useCallStore } from '@/state/callStore';
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

// 위험 수준별 색상/문구 (디자인: 주의=amber 기준, 안전=green, 높음=red)
const LEVEL_UI: Record<RiskLevel, { color: string; text: string; sub: string; desc: string }> = {
  safe: { color: '#34d399', text: '안전', sub: '안전', desc: '현재까지 위험 징후가 없습니다.' },
  warning: { color: '#f6a623', text: '주의', sub: '주의 필요', desc: '의심 징후가 탐지되었습니다. 주의가 필요합니다.' },
  danger: { color: '#ef4444', text: '높음', sub: '위험', desc: '보이스피싱 위험이 매우 높습니다. 통화 종료를 권장합니다.' },
};

const BULLET_COLORS = ['#f87171', '#fbbf24', '#4ade80'];

/** 섹션 카드(공통 테두리/배경) */
function Card({ children }: { children: React.ReactNode }) {
  return (
    <View
      style={{
        borderWidth: 1,
        borderColor: 'rgba(255,255,255,0.07)',
        borderRadius: 18,
        backgroundColor: 'rgba(255,255,255,0.02)',
        padding: 15,
        marginBottom: 16,
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

  const beginAnalysis = () => {
    setStarted(true);
    start();
  };

  const ui = LEVEL_UI[state.level];
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
  const latestWarnId = [...state.actions].reverse().find((a) => a.action_type === 'warn')?.id;
  const actionCards = state.actions
    .filter((a) => a.action_type !== 'followup_check' && (a.action_type !== 'warn' || a.id === latestWarnId))
    .reverse();

  const activeFollowupId = activeFollowup?.id;
  useEffect(() => {
    if (activeFollowupId == null || followupAlerted.current.has(activeFollowupId)) return;
    followupAlerted.current.add(activeFollowupId);
    alertFeedback('notice', vibration);
  }, [activeFollowupId, vibration]);

  // 세션 종료 → (마지막 발화 전사 대기) → 전사 로컬 저장 → 결과 화면. tab='chat'이면 대화 내용 탭으로.
  const endSession = async (tab?: 'chat') => {
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
    if (final.logId) router.replace(`/result?id=${final.logId}&score=${final.score}${tab ? `&tab=${tab}` : ''}`);
    else router.replace('/');
  };

  return (
    <View style={{ flex: 1, backgroundColor: '#05070d' }}>
      <StatusBar style="light" />
      <SafeAreaView edges={['top', 'bottom']} style={{ flex: 1 }}>
        {/* 상단 바: ‹ 종료 */}
        <View style={{ flexDirection: 'row', alignItems: 'center', paddingHorizontal: 16, paddingVertical: 8 }}>
          <Pressable hitSlop={10} onPress={() => endSession()} accessibilityLabel="분석 종료">
            <Icon name="chevron-left" size={26} color="#E8ECF2" />
          </Pressable>
        </View>

        <ScrollView style={{ flex: 1 }} contentContainerStyle={{ paddingHorizontal: 18, paddingBottom: 24 }} showsVerticalScrollIndicator={false}>
          {/* 헤더 로고 */}
          <View style={{ flexDirection: 'row', alignItems: 'center', gap: 9, paddingTop: 4, paddingBottom: 2 }}>
            <ShieldGlyph size={24} />
            <AppText weight="700" color="#eef2f9" style={{ fontSize: 17, letterSpacing: 0.2 }}>
              VoiceGuard AI
            </AppText>
          </View>

          {/* 타이틀 */}
          <AppText weight="800" color="#f6f8fc" style={{ fontSize: 32, textAlign: 'center', marginTop: 14, marginBottom: 20, letterSpacing: -0.5 }}>
            실시간 청취 분석
          </AppText>

          {/* 마이크 오브 */}
          <MicOrb color={ui.color} label="청취 중" />

          {/* LIVE 상태 pill */}
          <View style={{ alignItems: 'center', marginBottom: 18 }}>
            <View
              style={{
                flexDirection: 'row',
                alignItems: 'center',
                gap: 7,
                paddingHorizontal: 15,
                paddingVertical: 7,
                borderRadius: 999,
                backgroundColor: 'rgba(255,255,255,0.05)',
                borderWidth: 1,
                borderColor: 'rgba(255,255,255,0.09)',
              }}
            >
              <BlinkDot size={7} color="#34d399" />
              <AppText weight="600" color="#cdd4e0" style={{ fontSize: 13 }}>
                실시간 분석 중
              </AppText>
            </View>
          </View>

          {/* 위험 점수 카드 */}
          <View
            style={{
              borderWidth: 1.5,
              borderColor: rgba(ui.color, 0.55),
              borderRadius: 20,
              backgroundColor: rgba(ui.color, 0.06),
              padding: 18,
              flexDirection: 'row',
              alignItems: 'center',
              gap: 18,
              marginBottom: 16,
              shadowColor: ui.color,
              shadowOpacity: 0.3,
              shadowRadius: 20,
              shadowOffset: { width: 0, height: 0 },
            }}
          >
            <ScoreGauge score={score100} color={ui.color} sub={ui.sub} />
            <View style={{ flex: 1, minWidth: 0 }}>
              <View style={{ flexDirection: 'row', alignItems: 'center', gap: 6, marginBottom: 6 }}>
                <AppText weight="600" color="#c3cad6" style={{ fontSize: 14 }}>
                  보이스피싱 위험 점수
                </AppText>
              </View>
              <View style={{ flexDirection: 'row', alignItems: 'baseline', gap: 8, marginBottom: 8 }}>
                <AppText weight="800" style={{ fontSize: 26, color: ui.color }}>
                  {ui.text}
                </AppText>
                <View style={{ backgroundColor: rgba(ui.color, 0.16), paddingHorizontal: 9, paddingVertical: 3, borderRadius: 7 }}>
                  <AppText weight="700" style={{ fontSize: 13, color: ui.color }}>
                    {score100} / 100
                  </AppText>
                </View>
              </View>
              <AppText color="#98a1b0" style={{ fontSize: 12.5, lineHeight: 19 }}>
                {state.error ? `연결 오류: ${state.error}` : ui.desc}
              </AppText>
            </View>
          </View>

          {/* 사기 진행 단계 (상태 추적기) */}
          <Card>
            <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
              <AppText weight="700" color="#eef2f9" style={{ fontSize: 15 }}>
                사기 진행 단계
              </AppText>
              {state.stageIndex > 0 ? (
                <View style={{ backgroundColor: rgba(ui.color, 0.16), paddingHorizontal: 9, paddingVertical: 3, borderRadius: 7 }}>
                  <AppText weight="700" style={{ fontSize: 12, color: ui.color }}>
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
            <AppText color={state.summary ? '#c9d0dc' : '#6f7787'} style={{ fontSize: 13, lineHeight: 20, marginTop: 13 }}>
              {state.summary || '대화 흐름을 분석하고 있습니다. 사기 단계가 탐지되면 여기에 표시됩니다.'}
            </AppText>
          </Card>

          {/* AI 대응: 후속 확인 질문 + 에이전트가 결정한 행동 */}
          {activeFollowup || actionCards.length > 0 ? (
            <Card>
              <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8, marginBottom: 13 }}>
                <SparkleGlyph size={18} color="#8ab4ff" />
                <AppText weight="700" color="#eef2f9" style={{ fontSize: 15 }}>
                  AI 대응
                </AppText>
              </View>
              <View style={{ gap: 10 }}>
                {activeFollowup ? (
                  <FollowupCard
                    action={activeFollowup}
                    palette={DARK_AGENT_PALETTE}
                    onAnswer={(answer) => answerFollowup(activeFollowup.id, answer)}
                  />
                ) : null}
                {actionCards.map((action) => (
                  <ActionCard key={action.id} action={action} palette={DARK_AGENT_PALETTE} />
                ))}
              </View>
            </Card>
          ) : null}

          {/* 탐지된 주요 키워드 */}
          <Card>
            <AppText weight="700" color="#eef2f9" style={{ fontSize: 15, marginBottom: 13 }}>
              탐지된 주요 키워드
            </AppText>
            {keywords.length > 0 ? (
              <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8 }}>
                {keywords.slice(0, 8).map((k) => (
                  <View
                    key={k}
                    style={{
                      flexDirection: 'row',
                      alignItems: 'center',
                      gap: 6,
                      paddingHorizontal: 13,
                      paddingVertical: 8,
                      borderRadius: 10,
                      backgroundColor: 'rgba(255,255,255,0.05)',
                      borderWidth: 1,
                      borderColor: 'rgba(255,255,255,0.08)',
                    }}
                  >
                    <AppText weight="600" color="#dfe4ee" style={{ fontSize: 13 }}>
                      {k}
                    </AppText>
                    {recentSet.has(k) ? <View style={{ width: 6, height: 6, borderRadius: 3, backgroundColor: '#f6a623' }} /> : null}
                  </View>
                ))}
              </View>
            ) : (
              <AppText color="#6f7787" style={{ fontSize: 13 }}>
                아직 탐지된 키워드가 없습니다.
              </AppText>
            )}
          </Card>

          {/* 실시간 음성 텍스트 */}
          <Card>
            <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginBottom: 11 }}>
              <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
                <ChatGlyph size={18} color="#8b93a1" />
                <AppText weight="700" color="#eef2f9" style={{ fontSize: 15 }}>
                  실시간 음성 텍스트
                </AppText>
              </View>
              <View style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}>
                <BlinkDot size={7} color="#ef4444" period={1300} />
                <AppText weight="800" color="#ef5350" style={{ fontSize: 12, letterSpacing: 0.5 }}>
                  LIVE
                </AppText>
              </View>
            </View>
            {liveText ? (
              <HighlightedText
                text={liveText}
                keywords={lastTurn?.keywords ?? []}
                baseColor="#cdd4e0"
                highlightColor="#f6a623"
                weightHighlight="700"
                style={{ fontSize: 14.5, lineHeight: 24 }}
              />
            ) : (
              <AppText color="#6f7787" style={{ fontSize: 14, lineHeight: 22 }}>
                음성을 분석하고 있습니다…
              </AppText>
            )}
          </Card>

          {/* AI 분석 결과 */}
          <View
            style={{
              borderWidth: 1,
              borderColor: 'rgba(255,255,255,0.07)',
              borderRadius: 18,
              backgroundColor: 'rgba(255,255,255,0.02)',
              padding: 15,
              marginBottom: 16,
              position: 'relative',
              overflow: 'hidden',
            }}
          >
            <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8, marginBottom: 14 }}>
              <SparkleGlyph size={18} color="#8ab4ff" />
              <AppText weight="700" color="#eef2f9" style={{ fontSize: 15 }}>
                AI 분석 결과
              </AppText>
            </View>
            <AiNode />
            <View style={{ gap: 12, paddingRight: 80 }}>
              {bullets.length > 0 ? (
                bullets.map((b, i) => (
                  <View key={i} style={{ flexDirection: 'row', gap: 10, alignItems: 'flex-start' }}>
                    <View
                      style={{
                        marginTop: 6,
                        width: 8,
                        height: 8,
                        borderRadius: 4,
                        backgroundColor: BULLET_COLORS[i] ?? '#8ab4ff',
                        shadowColor: BULLET_COLORS[i] ?? '#8ab4ff',
                        shadowOpacity: 0.6,
                        shadowRadius: 6,
                      }}
                    />
                    <AppText color="#c9d0dc" style={{ fontSize: 13.5, lineHeight: 20, flex: 1 }}>
                      {b}
                    </AppText>
                  </View>
                ))
              ) : (
                <View style={{ flexDirection: 'row', gap: 10, alignItems: 'flex-start' }}>
                  <View style={{ marginTop: 6, width: 8, height: 8, borderRadius: 4, backgroundColor: '#4ade80' }} />
                  <AppText color="#c9d0dc" style={{ fontSize: 13.5, lineHeight: 20, flex: 1 }}>
                    현재까지 특별한 보이스피싱 위험 신호가 발견되지 않았습니다.
                  </AppText>
                </View>
              )}
            </View>
          </View>

          {/* 액션 버튼 */}
          <View style={{ flexDirection: 'row', gap: 9 }}>
            <ActionButton onPress={() => endSession()}>
              <StopGlyph size={18} color="#dfe4ee" />
              <AppText weight="600" color="#dfe4ee" style={{ fontSize: 12.5 }}>
                분석 종료
              </AppText>
            </ActionButton>
            <ActionButton onPress={() => endSession('chat')}>
              <FileGlyph size={18} color="#dfe4ee" />
              <AppText weight="600" color="#dfe4ee" style={{ fontSize: 12.5 }}>
                기록 보기
              </AppText>
            </ActionButton>
            <ActionButton highlight onPress={() => router.push('/prevention')}>
              <GuideGlyph size={18} color="#8ab4ff" />
              <AppText weight="700" color="#8ab4ff" style={{ fontSize: 12.5 }}>
                대응 가이드
              </AppText>
            </ActionButton>
          </View>
        </ScrollView>
      </SafeAreaView>

      {/* 시작 대기 오버레이: "분석 시작"을 누르기 전까지 마이크/분석을 열지 않는다 */}
      {!started ? (
        <View
          style={{
            position: 'absolute',
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            backgroundColor: 'rgba(5,7,13,0.94)',
          }}
        >
          <SafeAreaView edges={['top']} style={{ flex: 1 }}>
            <View style={{ flexDirection: 'row', alignItems: 'center', paddingHorizontal: 16, paddingVertical: 8 }}>
              <Pressable hitSlop={10} onPress={() => endSession()} accessibilityLabel="뒤로">
                <Icon name="chevron-left" size={26} color="#E8ECF2" />
              </Pressable>
            </View>
            <View style={{ flex: 1, alignItems: 'center', justifyContent: 'center', gap: 16, paddingBottom: 60 }}>
              <ShieldGlyph size={44} />
              <AppText weight="800" color="#f6f8fc" style={{ fontSize: 24 }}>
                실시간 청취 분석
              </AppText>
              <AppText color="#8794A4" style={{ fontSize: 13.5, textAlign: 'center', lineHeight: 21 }}>
                버튼을 누르면 마이크 청취와{'\n'}보이스피싱 위험도 분석이 시작됩니다
              </AppText>
              <Pressable
                onPress={beginAnalysis}
                accessibilityLabel="분석 시작"
                style={{
                  marginTop: 10,
                  paddingHorizontal: 48,
                  paddingVertical: 16,
                  borderRadius: 16,
                  backgroundColor: '#3f6fd0',
                }}
              >
                <AppText weight="800" color="#FFFFFF" style={{ fontSize: 16 }}>
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
            backgroundColor: 'rgba(5,7,13,0.82)',
            alignItems: 'center',
            justifyContent: 'center',
            gap: 14,
          }}
        >
          <ActivityIndicator size="large" color="#5b9bff" />
          <AppText weight="700" color="#eef2f9" style={{ fontSize: 15 }}>
            분석 마무리 중…
          </AppText>
          <AppText color="#8794A4" style={{ fontSize: 12.5 }}>
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
  highlight = false,
}: {
  children: React.ReactNode;
  onPress: () => void;
  highlight?: boolean;
}) {
  return (
    <Pressable
      onPress={onPress}
      style={{
        flex: 1,
        alignItems: 'center',
        gap: 5,
        paddingVertical: 13,
        paddingHorizontal: 4,
        borderRadius: 14,
        borderWidth: highlight ? 1.5 : 1,
        borderColor: highlight ? 'rgba(91,155,255,0.7)' : 'rgba(255,255,255,0.09)',
        backgroundColor: highlight ? 'rgba(63,111,208,0.16)' : 'rgba(255,255,255,0.04)',
      }}
    >
      {children}
    </Pressable>
  );
}
