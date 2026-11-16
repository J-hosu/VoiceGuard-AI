import { useEffect, useState } from 'react';
import { ActivityIndicator, Pressable, ScrollView, Share, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import {
  ActionCard,
  FollowupCard,
  StageStepper,
  TACTIC_LABELS,
  paletteFromTheme,
} from '@/components/agentCards';
import { AppText, balanced } from '@/components/AppText';
import { Card } from '@/components/Card';
import { HighlightedText } from '@/components/HighlightedText';
import { Icon } from '@/components/Icon';
import { IconCircle } from '@/components/IconCircle';
import { KeywordChip } from '@/components/KeywordChip';
import { PRIVACY_NOTICE } from '@/core/config/privacy';
import { useTheme } from '@/core/theme/theme';
import { extractKeywords } from '@/core/utils/keywords';
import { formatRelativeDate } from '@/core/utils/format';
import {
  riskColor,
  riskColorFaint,
  riskLabel,
  riskLevelFromScore,
  toPercent,
} from '@/core/utils/riskLevel';
import { evidenceBullets, recommendedAction } from '@/data/mock/infoContent';
import {
  fetchCallDetail,
  fetchTimeline,
  respondToAction,
  sendFeedback,
  type CallDetail,
  type FeedbackVerdict,
} from '@/data/services/callsApi';
import type { AgentAction, CallTimeline, TranscriptTurn } from '@/data/models/types';
import { useCallStore } from '@/state/callStore';
import { useSettingsStore } from '@/state/settingsStore';
import { isUnheardResult, useTranscriptStore } from '@/state/transcriptStore';

type Tab = 'summary' | 'chat' | 'evidence';
type TimelineStatus = 'loading' | 'done' | 'failed';

// 받아 적은 말이 없어 위험도를 판단하지 않은 통화의 머리 색(정상 초록·주의·위험 색과 구분되는 회색, 흰 글씨 대비 확보)
const UNHEARD_COLOR = '#5B6472';
// 타임라인(서버 발화)을 기다리는 상한. 넘기면 이 기기 기록으로 먼저 그리고, 늦게 오면 그때 고친다.
const TIMELINE_WAIT_MS = 4000;

/** 백엔드 목록 항목 + 상세를 합친 결과 화면 뷰모델 */
interface ResultView {
  category: string; // phishing_type
  score: number; // 0..1 (목록 risk_score)
  matchedPatterns: string[];
  coreEvidence: string;
  createdAt: string;
}

export default function Result() {
  const { colors } = useTheme();
  const router = useRouter();
  const params = useLocalSearchParams<{ id?: string; score?: string; tab?: string }>();
  const id = Number(params.id);
  const initialTab: Tab = params.tab === 'chat' || params.tab === 'evidence' ? params.tab : 'summary';
  // 분석 직후엔 목록 재조회 전이라 point가 없을 수 있어, 넘어온 score를 초기값으로 사용.
  const paramScore = Number(params.score);

  const calls = useCallStore((s) => s.calls);
  const loaded = useCallStore((s) => s.loaded);
  const fetchCalls = useCallStore((s) => s.fetchCalls);
  const dangerThreshold = useSettingsStore((s) => s.dangerThreshold);
  // 통화별 전사(로컬 저장). 통화 종료 시 realtime에서 logId별로 저장한 대화 전문. 빈 배열이면 받아 적은 말이 없던 통화.
  const hydrateTranscripts = useTranscriptStore((s) => s.hydrate);
  const clearEmptyTranscript = useTranscriptStore((s) => s.clearEmpty);
  const localTurns = useTranscriptStore((s) => (Number.isFinite(id) ? s.byLog[id] : undefined));
  const transcriptTurns = localTurns ?? [];

  const [tab, setTab] = useState<Tab>(initialTab);
  const [detail, setDetail] = useState<CallDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  // v2: 단계 전환·태그된 발화·에이전트 행동. 예전 통화나 구버전 백엔드면 비어 있다.
  const [timeline, setTimeline] = useState<CallTimeline | null>(null);
  // 타임라인을 받는 중인지. 서버 발화 수로 빈 통화를 가리므로, 받기 전에는 '정상 통화 0%'를 잠깐이라도 그리지 않는다.
  const [timelineStatus, setTimelineStatus] = useState<TimelineStatus>('loading');
  const [timelineWaitOver, setTimelineWaitOver] = useState(false);
  const [feedback, setFeedback] = useState<FeedbackVerdict | null>(null);

  useEffect(() => {
    setTimelineWaitOver(false);
    const timer = setTimeout(() => setTimelineWaitOver(true), TIMELINE_WAIT_MS);
    return () => clearTimeout(timer);
  }, [id]);

  useEffect(() => {
    let alive = true;
    if (!Number.isFinite(id)) {
      setTimelineStatus('failed');
      return;
    }
    setTimelineStatus('loading');
    fetchTimeline(id)
      .then((t) => {
        if (!alive) return;
        setTimeline(t);
        setTimelineStatus('done');
      })
      .catch(() => {
        // 타임라인이 없어도 결과 화면의 나머지는 그대로 보여준다.
        if (alive) setTimelineStatus('failed');
      });
    return () => {
      alive = false;
    };
  }, [id]);

  // 통화 후 후속 확인에 답하면 서버가 다음 행동(지급정지 안내 등)을 돌려준다.
  const answerFollowup = (actionId: number, answer: string) => {
    respondToAction(id, actionId, { response: 'answered', answer })
      .then(({ action, next_actions }) => {
        setTimeline((prev) =>
          prev
            ? {
                ...prev,
                actions: [...prev.actions.map((a) => (a.id === action.id ? action : a)), ...next_actions],
              }
            : prev,
        );
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  };

  const submitFeedback = (verdict: FeedbackVerdict) => {
    sendFeedback(id, verdict)
      .then(() => setFeedback(verdict))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  };

  // 목록이 아직 없으면 백엔드에서 받아온다(딥링크/새로고침 대비).
  useEffect(() => {
    if (!loaded) void fetchCalls();
  }, [loaded, fetchCalls]);

  // 로컬 전사 저장분 로드(앱 재시작/딥링크 대비. 같은 세션이면 이미 메모리에 있음).
  useEffect(() => {
    void hydrateTranscripts();
  }, [hydrateTranscripts]);

  // 상세(근거) 조회
  useEffect(() => {
    let alive = true;
    if (!Number.isFinite(id)) {
      setError('잘못된 통화 ID입니다.');
      setLoading(false);
      return;
    }
    setLoading(true);
    setError('');
    fetchCallDetail(id)
      .then((d) => {
        if (alive) setDetail(d);
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [id]);

  const listItem = calls.find((c) => c.id === id);

  const view: ResultView = {
    category: detail?.phishing_type || listItem?.phishing_type || '분석 결과',
    score: listItem?.risk_score ?? (Number.isFinite(paramScore) ? paramScore : 0),
    matchedPatterns: detail?.matched_patterns ?? [],
    coreEvidence: detail?.core_evidence ?? '',
    createdAt: listItem?.called_at ?? '',
  };

  const level = riskLevelFromScore(view.score, dangerThreshold);
  // 받아 적은 말이 없어 위험도를 매기지 않은 통화: 서버 발화가 0개면(타임라인을 못 받았으면 이 기기에 남긴 빈 통화 표시로) 가린다.
  // 이때는 '정상 통화 0%'·'위험 신호가 발견되지 않았습니다' 대신 판단하지 못했다고 보인다.
  // 점수가 0보다 크면 서버가 무언가를 분석한 것이므로(재전사로 발화를 바꾸는 중 등) 빈 통화로 보지 않는다.
  const heardNothing = isUnheardResult(localTurns, view.score, timeline ? timeline.messages.length : null);
  const headerColor = heardNothing ? UNHEARD_COLOR : riskColor(level, colors);
  const isRisk = !heardNothing && level !== 'safe';

  // 빈 통화로 기억했는데 서버에 발화가 있으면(응답을 받기 전에 끊긴 통화 등) 그 표시를 지워 히스토리와 이 화면을 맞춘다.
  const serverHasMessages = (timeline?.messages.length ?? 0) > 0;
  useEffect(() => {
    if (serverHasMessages && Number.isFinite(id)) clearEmptyTranscript(id);
  }, [serverHasMessages, id, clearEmptyTranscript]);

  const onShare = () => {
    void Share.share({
      message:
        `[VoiceGuard 분석 결과]\n` +
        `유형: ${view.category}\n` +
        `위험도: ${toPercent(view.score)}% (${riskLabel(level)})\n` +
        `근거: ${view.coreEvidence}\n` +
        `일시: ${formatRelativeDate(view.createdAt)}`,
    });
  };

  // 상세와 타임라인을 다 받은 뒤에 그린다(빈 통화가 '정상 통화 0%'로 잠깐 보이지 않게). 타임라인은 최대 4초만 기다린다.
  // 상세를 못 받으면 아래 오류 화면.
  if (!error && (loading || (timelineStatus === 'loading' && !timelineWaitOver))) {
    return (
      <View style={{ flex: 1, backgroundColor: colors.bg, alignItems: 'center', justifyContent: 'center' }}>
        <SafeAreaView edges={['top']} />
        <ActivityIndicator color={colors.primary} />
      </View>
    );
  }

  if (error && !detail) {
    return (
      <View style={{ flex: 1, backgroundColor: colors.bg, alignItems: 'center', justifyContent: 'center', padding: 24 }}>
        <SafeAreaView edges={['top']} />
        <AppText color={colors.danger} style={{ fontSize: 14, textAlign: 'center', lineHeight: 21 }}>
          통화 상세를 불러오지 못했습니다.{'\n'}{error}
        </AppText>
        <Pressable onPress={() => router.replace('/')} style={{ marginTop: 16 }}>
          <AppText weight="700" color={colors.primary} style={{ fontSize: 15 }}>
            홈으로
          </AppText>
        </Pressable>
      </View>
    );
  }

  return (
    <View style={{ flex: 1, backgroundColor: colors.bg }}>
      <StatusBar style="light" />
      {/* 헤더 */}
      <View style={{ backgroundColor: headerColor }}>
        <SafeAreaView edges={['top']}>
          <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingHorizontal: 18, paddingTop: 4 }}>
            <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
              <Pressable hitSlop={10} onPress={() => (router.canGoBack() ? router.back() : router.replace('/'))}>
                <Icon name="chevron-left" size={26} color="#FFFFFF" />
              </Pressable>
              <AppText weight="800" color="#FFFFFF" style={{ fontSize: 19 }}>
                분석 결과
              </AppText>
            </View>
            {/* 판단하지 않은 통화는 공유할 결과가 없다 */}
            {heardNothing ? null : (
              <Pressable hitSlop={10} onPress={onShare} accessibilityLabel="공유">
                <Icon name="share" size={22} color="#FFFFFF" />
              </Pressable>
            )}
          </View>
          {heardNothing ? (
            <View style={{ alignItems: 'center', paddingTop: 16, paddingBottom: 38, paddingHorizontal: 24, gap: 10 }}>
              <Icon name="volume-x" size={30} color="#FFFFFF" />
              <AppText weight="800" color="#FFFFFF" style={[{ fontSize: 22, lineHeight: 30, textAlign: 'center' }, balanced]}>
                들은 말이 없어 판단하지 못했어요
              </AppText>
            </View>
          ) : (
            <View style={{ alignItems: 'center', paddingTop: 12, paddingBottom: 34 }}>
              <View style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}>
                <Icon name={isRisk ? 'shield-alert' : 'shield-check'} size={16} color="#FFFFFF" />
                <AppText weight="600" color="#FFFFFF" style={{ fontSize: 13.5 }}>
                  {isRisk ? '보이스피싱 의심' : '정상 통화'}
                </AppText>
              </View>
              <AppText weight="800" color="#FFFFFF" style={{ fontSize: 54, letterSpacing: -1, marginTop: 2 }}>
                {toPercent(view.score)}%
              </AppText>
              <AppText weight="700" color="#FFFFFF" style={{ fontSize: 15, marginTop: 2 }}>
                {view.category}
              </AppText>
            </View>
          )}
        </SafeAreaView>
      </View>

      <ScrollView style={{ flex: 1, marginTop: -20 }} contentContainerStyle={{ padding: 18, paddingTop: 0 }} showsVerticalScrollIndicator={false}>
        <Card padding={15} style={{ borderRadius: 22 }}>
          {/* 탭 */}
          <View style={{ flexDirection: 'row', gap: 8, marginBottom: 15 }}>
            {([
              ['summary', '요약'],
              ['chat', '대화 내용'],
              ['evidence', '탐지 근거'],
            ] as const).map(([key, label]) => {
              const active = tab === key;
              return (
                <Pressable key={key} style={{ flex: 1 }} onPress={() => setTab(key)}>
                  <View
                    style={{
                      paddingVertical: 9,
                      borderRadius: 11,
                      alignItems: 'center',
                      backgroundColor: active ? colors.primaryFaint : 'transparent',
                      borderWidth: active ? 0 : 1,
                      borderColor: colors.border,
                    }}
                  >
                    <AppText weight={active ? '700' : '600'} color={active ? colors.primary : colors.textSecondary} style={{ fontSize: 13.5 }}>
                      {label}
                    </AppText>
                  </View>
                </Pressable>
              );
            })}
          </View>

          {tab === 'summary' ? (
            <View style={{ gap: 15 }}>
              {timeline ? (
                <AgentSection timeline={timeline} color={headerColor} onAnswer={answerFollowup} />
              ) : null}
              {/* 판단하지 않은 통화에는 '판정이 맞았나요?'를 묻지 않는다(맞고 틀릴 판정이 없다) */}
              {heardNothing ? (
                <UnheardNotice />
              ) : (
                <>
                  <SummaryTab view={view} isRisk={isRisk} />
                  <FeedbackBox isRisk={isRisk} sent={feedback} onSend={submitFeedback} />
                </>
              )}
            </View>
          ) : null}
          {tab === 'chat' ? (
            <ChatTab turns={transcriptTurns} tagged={timeline?.messages ?? []} serverStatus={timelineStatus} />
          ) : null}
          {tab === 'evidence' ? heardNothing ? <UnheardNotice /> : <EvidenceTab view={view} level={level} /> : null}
        </Card>
      </ScrollView>
    </View>
  );
}

/** 요약 탭 상단: 도달한 사기 단계, 통화 후 후속 확인, 에이전트가 실행한 대응 기록. */
function AgentSection({
  timeline,
  color,
  onAnswer,
}: {
  timeline: CallTimeline;
  color: string;
  onAnswer: (actionId: number, answer: string) => void;
}) {
  const { colors } = useTheme();
  const palette = paletteFromTheme(colors);
  const reachedIndex = timeline.stages.reduce((max, s) => Math.max(max, s.stage_index), 0);
  // 통화 후 후속 확인이 가장 최근 질문이다. 답하지 않은 것이 있으면 그것을 먼저 보여준다.
  const followups = timeline.actions.filter((a) => a.action_type === 'followup_check');
  const pending = followups.filter((a) => a.status === 'delivered' && a.payload.phase === 'post_call').pop();
  const answered = followups.filter((a) => a.status === 'answered').pop();
  const followup = pending ?? answered;
  // 피해 발생 시의 긴급 조치(지급정지 등)는 기록 목록에 묻히지 않게 맨 위에 따로 둔다.
  const urgent = timeline.actions.filter((a: AgentAction) => a.action_type === 'block_guide');
  const actions = timeline.actions.filter(
    (a: AgentAction) => a.action_type !== 'followup_check' && a.action_type !== 'block_guide',
  );

  const [showAll, setShowAll] = useState(false);
  const COLLAPSED_COUNT = 2;
  const shownActions = showAll ? actions : actions.slice(0, COLLAPSED_COUNT);

  if (reachedIndex === 0 && actions.length === 0 && urgent.length === 0 && !followup) return null;

  return (
    <View style={{ gap: 14 }}>
      {followup ? (
        <FollowupCard action={followup} palette={palette} onAnswer={(answer) => onAnswer(followup.id, answer)} />
      ) : null}

      {urgent.map((action) => (
        <ActionCard key={action.id} action={action} palette={palette} />
      ))}

      {reachedIndex > 0 ? (
        <View style={{ borderWidth: 1, borderColor: colors.border, borderRadius: 16, padding: 16 }}>
          <AppText weight="800" style={{ fontSize: 16, marginBottom: 14 }}>
            사기 진행 단계
          </AppText>
          <StageStepper stageIndex={reachedIndex} color={color} palette={palette} />
        </View>
      ) : null}

      {actions.length > 0 ? (
        <View style={{ gap: 10 }}>
          <View style={{ flexDirection: 'row', alignItems: 'baseline', gap: 8, marginTop: 4 }}>
            <AppText weight="800" style={{ fontSize: 16 }}>
              AI 대응 기록
            </AppText>
            <AppText weight="700" color={colors.textMuted} style={{ fontSize: 13 }}>
              {actions.length}건
            </AppText>
          </View>
          {shownActions.map((action) => (
            <ActionCard key={action.id} action={action} palette={palette} />
          ))}
          {actions.length > COLLAPSED_COUNT ? (
            <Pressable
              onPress={() => setShowAll((v) => !v)}
              accessibilityRole="button"
              style={{
                alignSelf: 'center',
                paddingVertical: 9,
                paddingHorizontal: 16,
                borderRadius: 999,
                borderWidth: 1,
                borderColor: colors.border,
              }}
            >
              <AppText weight="700" color={colors.textSecondary} style={{ fontSize: 13 }}>
                {showAll ? '접기' : `나머지 ${actions.length - COLLAPSED_COUNT}건 더 보기`}
              </AppText>
            </Pressable>
          ) : null}
        </View>
      ) : null}
    </View>
  );
}

/** 판정 피드백. 오탐 신고와 놓친 피싱 신고가 쌓여 탐지 개선에 쓰인다. */
function FeedbackBox({
  isRisk,
  sent,
  onSend,
}: {
  isRisk: boolean;
  sent: FeedbackVerdict | null;
  onSend: (verdict: FeedbackVerdict) => void;
}) {
  const { colors } = useTheme();
  const options: [FeedbackVerdict, string][] = isRisk
    ? [
        ['confirmed_phishing', '실제 피싱이었어요'],
        ['false_positive', '정상 통화였어요'],
      ]
    : [
        ['missed_phishing', '사실은 피싱이었어요'],
        ['unsure', '잘 모르겠어요'],
      ];

  return (
    <View style={{ borderWidth: 1, borderColor: colors.border, borderRadius: 16, padding: 16 }}>
      <AppText weight="800" style={{ fontSize: 15 }}>
        이 판정이 맞았나요?
      </AppText>
      <AppText color={colors.textMuted} style={{ fontSize: 12, marginTop: 3, marginBottom: 12 }}>
        알려주신 내용은 탐지 정확도를 높이는 데 쓰입니다.
      </AppText>
      {sent ? (
        <AppText weight="600" color={colors.textSecondary} style={{ fontSize: 13 }}>
          의견을 보냈습니다. 감사합니다.
        </AppText>
      ) : (
        <View style={{ flexDirection: 'row', gap: 8 }}>
          {options.map(([verdict, label]) => (
            <Pressable key={verdict} style={{ flex: 1 }} onPress={() => onSend(verdict)} accessibilityLabel={label}>
              <View
                style={{
                  paddingVertical: 10,
                  borderRadius: 11,
                  alignItems: 'center',
                  backgroundColor: colors.cardAlt,
                }}
              >
                <AppText weight="700" color={colors.text} style={{ fontSize: 13 }}>
                  {label}
                </AppText>
              </View>
            </Pressable>
          ))}
        </View>
      )}
    </View>
  );
}

function SummaryTab({ view, isRisk }: { view: ResultView; isRisk: boolean }) {
  const { colors } = useTheme();
  const bullets = isRisk ? evidenceBullets(view.matchedPatterns) : [];

  return (
    <View style={{ gap: 15 }}>
      <View style={{ borderWidth: 1, borderColor: colors.border, borderRadius: 16, padding: 16 }}>
        <AppText weight="800" style={{ fontSize: 16, marginBottom: 14 }}>
          핵심 근거
        </AppText>
        {isRisk ? (
          <View style={{ gap: 14 }}>
            {bullets.map((b, i) => (
              <View key={i} style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
                <IconCircle name="alert-octagon" color={colors.danger} bg={colors.dangerFaint} size={38} radius={10} iconSize={19} />
                <AppText weight="700" style={{ fontSize: 14.5, flex: 1 }}>
                  {b}
                </AppText>
              </View>
            ))}
            <AppText color={colors.textSecondary} style={{ fontSize: 13, lineHeight: 20, marginTop: 2 }}>
              {view.coreEvidence || '분석된 위험 근거가 없습니다.'}
            </AppText>
          </View>
        ) : (
          <View style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
            <IconCircle name="check-circle" color={colors.safe} bg={colors.safeFaint} size={38} radius={10} iconSize={19} />
            <AppText weight="600" color={colors.textSecondary} style={{ fontSize: 13.5, flex: 1 }}>
              특별한 보이스피싱 위험 신호가 발견되지 않았습니다.
            </AppText>
          </View>
        )}
      </View>

      <View style={{ backgroundColor: colors.primaryFaint, borderRadius: 14, padding: 15 }}>
        <View style={{ flexDirection: 'row', alignItems: 'center', gap: 7, marginBottom: 6 }}>
          <Icon name="volume" size={17} color={colors.primary} />
          <AppText weight="800" color={colors.primary} style={{ fontSize: 14 }}>
            권장 행동
          </AppText>
        </View>
        <AppText weight="500" color={colors.primary} style={{ fontSize: 13, lineHeight: 20 }}>
          {isRisk
            ? recommendedAction(view.category)
            : '정상적인 통화로 보입니다. 다만 개인정보·인증번호 요구에는 항상 주의하세요.'}
        </AppText>
      </View>
    </View>
  );
}

/** 받아 적은 말이 없던 통화: 위험도를 매기지 않았다는 안내(요약·탐지 근거 탭 공용) */
function UnheardNotice() {
  const { colors } = useTheme();
  return (
    <View style={{ borderWidth: 1, borderColor: colors.border, borderRadius: 16, padding: 16, gap: 10 }}>
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
        <IconCircle name="volume-x" color={colors.textMuted} bg={colors.cardAlt} size={38} radius={10} iconSize={19} />
        <AppText weight="700" style={{ fontSize: 15, lineHeight: 22, flex: 1 }}>
          받아 적은 말이 없어요
        </AppText>
      </View>
      <AppText color={colors.textSecondary} style={{ fontSize: 13, lineHeight: 20 }}>
        보이스피싱 위험도는 매기지 않았어요. 마이크가 켜져 있는지, 통화 소리가 잘 들리는지 확인한 뒤 다시 분석해 주세요.
      </AppText>
    </View>
  );
}

/**
 * 대화 내용(전사) 탭.
 * 서버가 저장한 발화(GET /calls/{id}/timeline의 messages)가 있으면 역할·수법 태그와 함께 그리고,
 * 없으면 통화 종료 때 이 기기에 저장해 둔 전사(turns)를 화자별로 그린다. 둘 다 없으면 안내문을 보여준다.
 */
function ChatTab({
  turns,
  tagged,
  serverStatus,
}: {
  turns: TranscriptTurn[];
  tagged: CallTimeline['messages'];
  serverStatus: TimelineStatus; // 서버 발화를 아직 받는 중이거나 못 받았으면 '저장된 대화가 없다'고 하지 않는다
}) {
  const { colors } = useTheme();

  // 서버가 발화별 역할과 수법 태그를 갖고 있으면 그것으로 그린다(어느 말이 왜 위험한지 표시).
  if (tagged.length > 0) {
    // 통화 후 전체 재전사·녹음 파일 분석이 화자를 나눴으면(STT_FILE_ENGINE=rtzr) 저장된 전사의 화자A/화자B는
    // 서버가 화자 번호별로 추적기 역할을 다수결해 사람 단위로 정한 쪽이다. 발화마다 붙은 역할을 그대로 쓰면 한 사람의 말이
    // 양쪽으로 갈리므로, 저장된 전사가 서버 발화와 하나하나 같고 두 쪽이 모두 있을 때만 그 쪽을 따른다.
    // (실시간 조각을 그대로 저장한 통화는 모두 화자A라 여기에 해당하지 않고 추적기 역할로 그린다)
    const bySpeaker =
      turns.length === tagged.length &&
      turns.some((t) => t.role === '화자A') &&
      turns.some((t) => t.role === '화자B') &&
      turns.every((t, i) => t.content.trim() === tagged[i].content.trim());
    return (
      <View style={{ gap: 12 }}>
        {tagged.map((m, i) => {
          const isRight = bySpeaker ? turns[i].role === '화자B' : m.role === 'user';
          const tactics = m.tactics.filter((t) => TACTIC_LABELS[t]);
          return (
            <View key={m.message_id} style={{ alignItems: isRight ? 'flex-end' : 'flex-start' }}>
              <AppText color={colors.textMuted} style={{ fontSize: 11, marginBottom: 4 }}>
                {isRight ? '나' : '상대방'}
              </AppText>
              <View
                style={{
                  maxWidth: '82%',
                  backgroundColor: isRight ? colors.primaryFaint : colors.cardAlt,
                  paddingHorizontal: 13,
                  paddingVertical: 10,
                  borderRadius: 14,
                  borderTopRightRadius: isRight ? 4 : 14,
                  borderTopLeftRadius: isRight ? 14 : 4,
                  borderWidth: tactics.some((t) => !TACTIC_LABELS[t].safe) ? 1 : 0,
                  borderColor: colors.danger,
                }}
              >
                <AppText color={colors.text} style={{ fontSize: 14, lineHeight: 21 }}>
                  {m.content}
                </AppText>
              </View>
              {tactics.length > 0 ? (
                <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 5, marginTop: 5, maxWidth: '82%' }}>
                  {tactics.map((t) => {
                    const safe = TACTIC_LABELS[t].safe;
                    return (
                      <View
                        key={t}
                        style={{
                          paddingHorizontal: 8,
                          paddingVertical: 3,
                          borderRadius: 7,
                          backgroundColor: safe ? colors.safeFaint : colors.dangerFaint,
                        }}
                      >
                        <AppText weight="700" color={safe ? colors.safe : colors.danger} style={{ fontSize: 11 }}>
                          {TACTIC_LABELS[t].label}
                        </AppText>
                      </View>
                    );
                  })}
                </View>
              ) : null}
            </View>
          );
        })}
      </View>
    );
  }

  if (turns.length === 0) {
    return (
      <View style={{ alignItems: 'center', paddingVertical: 26, gap: 8 }}>
        <IconCircle name="file" color={colors.textMuted} bg={colors.cardAlt} size={44} radius={12} iconSize={22} />
        <AppText weight="700" color={colors.textSecondary} style={{ fontSize: 14 }}>
          {serverStatus === 'loading'
            ? '대화 내용을 불러오는 중이에요'
            : serverStatus === 'failed'
              ? '대화 내용을 불러오지 못했어요'
              : '이 통화에 저장된 대화가 없어요'}
        </AppText>
        {serverStatus === 'loading' ? null : (
          <AppText color={colors.textMuted} style={{ fontSize: 12.5, textAlign: 'center', lineHeight: 19 }}>
            {serverStatus === 'failed' ? '인터넷 연결을 확인하고 다시 열어 주세요.' : PRIVACY_NOTICE}
          </AppText>
        )}
      </View>
    );
  }

  // 백엔드 화자A/B → 상대방/나로 표시(좌=상대방, 우=나).
  // ※ 단일 마이크라 실제 '나'를 음성으로 식별할 수 없어, 백엔드 등장순 라벨(화자A=먼저 말한 사람)을
  //   상대방, 화자B를 나로 두는 근사 매핑이다. 청크마다 라벨이 바뀔 수 있어 완벽하지 않다.
  const meta = (t: TranscriptTurn) => {
    if (t.role?.startsWith('화자')) {
      const isMe = t.role === '화자B';
      return { isRight: isMe, label: isMe ? '나' : '상대방' };
    }
    return { isRight: t.isMine, label: t.isMine ? '나' : '상대방' };
  };

  return (
    <View style={{ gap: 12 }}>
      {turns.map((t) => {
        const { isRight, label } = meta(t);
        return (
          <View key={t.turnIndex} style={{ alignItems: isRight ? 'flex-end' : 'flex-start' }}>
            <AppText color={colors.textMuted} style={{ fontSize: 11, marginBottom: 4 }}>
              {label}
            </AppText>
            <View
              style={{
                maxWidth: '82%',
                backgroundColor: isRight ? colors.primaryFaint : colors.cardAlt,
                paddingHorizontal: 13,
                paddingVertical: 10,
                borderRadius: 14,
                borderTopRightRadius: isRight ? 4 : 14,
                borderTopLeftRadius: isRight ? 14 : 4,
              }}
            >
              <AppText color={colors.text} style={{ fontSize: 14, lineHeight: 21 }}>
                {t.content}
              </AppText>
            </View>
          </View>
        );
      })}
    </View>
  );
}

/** 받침이 없거나 ㄹ받침이면 '로', 그 밖에는 '으로' */
function withRo(word: string): string {
  const text = word.trim();
  const code = text.charCodeAt(text.length - 1) - 0xac00;
  if (Number.isNaN(code) || code < 0 || code > 11171) return '(으)로';
  const jong = code % 28;
  return jong === 0 || jong === 8 ? '로' : '으로';
}

function EvidenceTab({ view, level }: { view: ResultView; level: 'safe' | 'warning' | 'danger' }) {
  const { colors } = useTheme();
  const main = riskColor(level, colors);
  // 백엔드는 단어 위치를 주지 않으므로 핵심근거 문장에서 키워드를 근사 추출.
  const keywords = extractKeywords(view.coreEvidence, 6);
  const showSignals = level !== 'safe';

  return (
    <View style={{ gap: 22 }}>
      {/* 범죄 유형 */}
      <View>
        <AppText weight="800" style={{ fontSize: 18 }}>
          범죄 유형
        </AppText>
        <View
          style={{
            backgroundColor: riskColorFaint(level, colors),
            borderRadius: 16,
            padding: 18,
            marginTop: 12,
            flexDirection: 'row',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 14,
          }}
        >
          <AppText weight="700" style={{ fontSize: 15, lineHeight: 22, flex: 1 }}>
            {level === 'safe' ? (
              <>해당 통화는{'\n'}정상 통화로{'\n'}보입니다</>
            ) : (
              <>해당 통화는{'\n'}"{view.category}"{withRo(view.category)}{'\n'}의심됩니다</>
            )}
          </AppText>
          <View
            style={{
              width: 66,
              height: 66,
              borderRadius: 33,
              backgroundColor: main,
              alignItems: 'center',
              justifyContent: 'center',
            }}
          >
            <AppText weight="600" color="#FFFFFF" style={{ fontSize: 11 }}>
              {riskLabel(level)}
            </AppText>
            <AppText weight="800" color="#FFFFFF" style={{ fontSize: 22 }}>
              {toPercent(view.score)}
            </AppText>
          </View>
        </View>
      </View>

      {/* 탐지된 패턴 — 최종 판단이 정상이면 보이지 않는다(규칙에 걸린 표현이 '위험 패턴'처럼 보여 판정과 어긋난다) */}
      {showSignals && view.matchedPatterns.length > 0 ? (
        <View>
          <AppText weight="800" style={{ fontSize: 18 }}>
            탐지된 위험 패턴
          </AppText>
          <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 10, marginTop: 14 }}>
            {view.matchedPatterns.map((p) => (
              <KeywordChip key={p} text={p} />
            ))}
          </View>
        </View>
      ) : null}

      {/* 의심되는 주요 단어 (근거 문장에서 근사) — 정상 판정의 근거 문장("송금 요구는 없다")에서 뽑으면 거꾸로 읽히므로 숨긴다 */}
      {showSignals && keywords.length > 0 ? (
        <View>
          <AppText weight="800" style={{ fontSize: 18 }}>
            의심되는 주요 단어
          </AppText>
          <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 10, marginTop: 14 }}>
            {keywords.map((k) => (
              <KeywordChip key={k} text={k} />
            ))}
          </View>
        </View>
      ) : null}

      {/* 핵심 근거 문장 */}
      {view.coreEvidence ? (
        <View>
          <AppText weight="800" style={{ fontSize: 18 }}>
            핵심 근거
          </AppText>
          <View style={{ backgroundColor: colors.cardAlt, borderRadius: 16, padding: 16, marginTop: 14 }}>
            <HighlightedText
              text={view.coreEvidence}
              keywords={showSignals ? keywords : []}
              baseColor={colors.textSecondary}
              highlightColor={colors.text}
              weightHighlight="700"
              style={{ fontSize: 14, lineHeight: 24 }}
            />
          </View>
        </View>
      ) : null}
    </View>
  );
}
