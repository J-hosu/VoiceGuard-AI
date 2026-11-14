import { useCallback, type ReactNode } from 'react';
import { Linking, Platform, Pressable, ScrollView, View, useWindowDimensions } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useFocusEffect, useRouter } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { AppText, keepAll } from '@/components/AppText';
import { Card } from '@/components/Card';
import { Icon } from '@/components/Icon';
import { IconCircle } from '@/components/IconCircle';
import { ChatGlyph } from '@/components/realtimeAnalysis';
import { BASE_URL_HTTP } from '@/core/config/env';
import { useTheme } from '@/core/theme/theme';
import { formatRelativeDate } from '@/core/utils/format';
import {
  riskColor,
  riskColorFaint,
  riskColorText,
  riskLabel,
  riskLevelFromScore,
  toPercent,
} from '@/core/utils/riskLevel';
import { useCallStore } from '@/state/callStore';
import { useSettingsStore } from '@/state/settingsStore';

/** 카드 안 구분선(좌우 16 들여쓰기) */
function RowDivider() {
  const { colors } = useTheme();
  return <View style={{ height: 1, marginHorizontal: 16, backgroundColor: colors.border }} />;
}

/**
 * 직접 확인하는 도구 한 줄 (아이콘 + 제목/설명 + 화살표). 한 카드 안에 구분선으로 묶어,
 * 채운 버튼은 '실시간 분석 시작' 하나만 남게 한다. 제목은 이동할 화면의 제목과 같다.
 */
function ToolRow({
  icon,
  title,
  desc,
  divider,
  onPress,
}: {
  icon: ReactNode;
  title: string;
  desc: string;
  divider: boolean;
  onPress: () => void;
}) {
  const { colors } = useTheme();
  return (
    <Pressable
      onPress={onPress}
      android_ripple={{ color: 'rgba(0,0,0,0.05)' }}
      accessibilityRole="button"
      accessibilityLabel={`${title}, ${desc}`}
      style={({ pressed }) => ({ backgroundColor: pressed ? colors.cardAlt : 'transparent' })}
    >
      {divider ? <RowDivider /> : null}
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 12, minHeight: 68, paddingHorizontal: 16, paddingVertical: 12 }}>
        <View
          style={{
            width: 40,
            height: 40,
            borderRadius: 12,
            backgroundColor: colors.primaryFaint,
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          {icon}
        </View>
        <View style={{ flex: 1 }}>
          <AppText weight="700" style={{ fontSize: 15, lineHeight: 22 }}>
            {title}
          </AppText>
          <AppText color={colors.textSecondary} style={[{ fontSize: 14, lineHeight: 20 }, keepAll]}>
            {desc}
          </AppText>
        </View>
        <Icon name="chevron-right" size={20} color={colors.textMuted} />
      </View>
    </Pressable>
  );
}

/** 최근 분석 내역 한 줄 (한 카드 안에 구분선으로 묶어 보여준다) */
function RecentRow({
  category,
  score,
  createdAt,
  divider,
  onPress,
}: {
  category: string;
  score: number; // 0..1
  createdAt: string; // ISO
  divider: boolean;
  onPress: () => void;
}) {
  const { colors } = useTheme();
  const dangerThreshold = useSettingsStore((s) => s.dangerThreshold);
  const level = riskLevelFromScore(score, dangerThreshold);
  const mainText = riskColorText(level, colors);

  return (
    <Pressable
      onPress={onPress}
      android_ripple={{ color: 'rgba(0,0,0,0.05)' }}
      accessibilityRole="button"
      style={({ pressed }) => ({ backgroundColor: pressed ? colors.cardAlt : 'transparent' })}
    >
      {divider ? <RowDivider /> : null}
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 12, minHeight: 68, paddingHorizontal: 16, paddingVertical: 12 }}>
        <IconCircle
          name={level === 'safe' ? 'shield-check' : 'shield-alert'}
          color={riskColor(level, colors)}
          bg={riskColorFaint(level, colors)}
          size={40}
          radius={20}
          iconSize={20}
        />
        <View style={{ flex: 1 }}>
          <AppText weight="700" style={{ fontSize: 15, lineHeight: 22 }} numberOfLines={1}>
            {category}
          </AppText>
          <AppText color={colors.textSecondary} style={{ fontSize: 13, lineHeight: 18 }}>
            {formatRelativeDate(createdAt)}
          </AppText>
        </View>
        <View style={{ alignItems: 'flex-end' }}>
          <AppText weight="800" color={mainText} style={{ fontSize: 17, lineHeight: 22 }}>
            {toPercent(score)}%
          </AppText>
          <AppText weight="600" color={mainText} style={{ fontSize: 12, lineHeight: 18 }}>
            {riskLabel(level)}
          </AppText>
        </View>
      </View>
    </Pressable>
  );
}

/** 최근 내역을 처음 불러오는 동안의 자리 표시 줄 */
function SkeletonRow({ divider }: { divider: boolean }) {
  const { colors } = useTheme();
  return (
    <View>
      {divider ? <RowDivider /> : null}
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 12, minHeight: 68, paddingHorizontal: 16, paddingVertical: 12 }}>
        <View style={{ width: 40, height: 40, borderRadius: 20, backgroundColor: colors.cardAlt }} />
        <View style={{ flex: 1, gap: 8 }}>
          <View style={{ width: 120, height: 12, borderRadius: 6, backgroundColor: colors.cardAlt }} />
          <View style={{ width: 72, height: 10, borderRadius: 5, backgroundColor: colors.cardAlt }} />
        </View>
      </View>
    </View>
  );
}

export default function Home() {
  const { colors, isDark } = useTheme();
  const router = useRouter();
  const calls = useCallStore((s) => s.calls);
  const loaded = useCallStore((s) => s.loaded);
  const loading = useCallStore((s) => s.loading);
  const loadError = useCallStore((s) => s.error);
  const fetchCalls = useCallStore((s) => s.fetchCalls);
  const dangerThreshold = useSettingsStore((s) => s.dangerThreshold);
  // 다크 모드의 밝은 파랑 위 흰 글씨는 대비가 낮아, 파랑으로 채운 면은 한 단계 진한 파랑을 쓴다.
  const brandFill = isDark ? colors.primaryDark : colors.primary;

  // 홈에 돌아올 때마다 최신 히스토리 반영
  useFocusEffect(
    useCallback(() => {
      void fetchCalls();
    }, [fetchCalls]),
  );

  const now = new Date();
  const monthCalls = calls.filter((c) => {
    const d = new Date(c.called_at);
    return d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth();
  });
  const monthDanger = monthCalls.filter((c) => riskLevelFromScore(c.risk_score, dangerThreshold) !== 'safe').length;
  // 아직 받은 목록이 없는데 불러오는 중이면(실패 후 다시 불러올 때 포함) 숫자 대신 '불러오는 중'을 보인다.
  const pending = !loaded || (loading && calls.length === 0);
  // 목록을 못 불러왔으면 "0건"처럼 안심시키는 숫자를 보여주지 않는다.
  const unavailable = !pending && Boolean(loadError) && calls.length === 0;
  // 수사 지원 대시보드 링크는 발표용 넓은 화면에서만 보인다(일반 사용자 화면에는 두지 않는다).
  const { width } = useWindowDimensions();
  const showDashboardLink = Platform.OS === 'web' && width >= 1180;

  const recent = calls.slice(0, 3);

  return (
    <View style={{ flex: 1, backgroundColor: colors.bg }}>
      <StatusBar style="light" />
      {/* 파란 헤더: 로고 + 이번 달 요약 */}
      <View style={{ backgroundColor: brandFill }}>
        <SafeAreaView edges={['top']}>
          <View style={{ paddingHorizontal: 24 }}>
            <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', height: 52 }}>
              <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
                <Icon name="shield-check" size={24} color="#FFFFFF" />
                <AppText weight="800" color="#FFFFFF" style={{ fontSize: 19, letterSpacing: -0.4 }}>
                  VoiceGuard AI
                </AppText>
              </View>
              <Pressable
                onPress={() => router.push('/settings')}
                accessibilityRole="button"
                accessibilityLabel="설정"
                style={{ width: 44, height: 44, marginRight: -10, alignItems: 'center', justifyContent: 'center' }}
              >
                <Icon name="settings" size={22} color="#FFFFFF" />
              </Pressable>
            </View>
            <View style={{ paddingTop: 12, paddingBottom: 56 }}>
              <AppText weight="600" color="rgba(255,255,255,0.92)" style={{ fontSize: 15, lineHeight: 22 }}>
                이번 달 찾아낸 의심 통화
              </AppText>
              {unavailable ? (
                <View style={{ marginTop: 8, gap: 2 }}>
                  <AppText weight="700" color="#FFFFFF" style={{ fontSize: 17, lineHeight: 24 }}>
                    기록을 불러오지 못했어요
                  </AppText>
                  <AppText weight="500" color="rgba(255,255,255,0.92)" style={{ fontSize: 13, lineHeight: 18 }}>
                    인터넷 연결을 확인해 주세요
                  </AppText>
                </View>
              ) : (
                <>
                  <View style={{ flexDirection: 'row', alignItems: 'baseline', marginTop: 4 }}>
                    <AppText weight="800" color="#FFFFFF" style={{ fontSize: 40, lineHeight: 48, letterSpacing: -1 }}>
                      {pending ? '–' : monthDanger}
                    </AppText>
                    <AppText weight="700" color="#FFFFFF" style={{ fontSize: 20, lineHeight: 28, marginLeft: 2 }}>
                      건
                    </AppText>
                  </View>
                  <AppText weight="500" color="rgba(255,255,255,0.92)" style={{ fontSize: 14, lineHeight: 20, marginTop: 4 }}>
                    {pending ? '불러오는 중…' : `분석한 통화 ${monthCalls.length}건 중`}
                  </AppText>
                </>
              )}
            </View>
          </View>
        </SafeAreaView>
      </View>

      <ScrollView
        style={{ flex: 1, marginTop: -40 }}
        contentContainerStyle={{ paddingHorizontal: 16, paddingBottom: 24, gap: 12 }}
        showsVerticalScrollIndicator={false}
      >
        {/* 주 진입: 실시간 분석 시작 (이 화면에서 유일하게 채운 버튼) */}
        <Pressable
          onPress={() => router.push('/realtime')}
          android_ripple={{ color: 'rgba(0,0,0,0.05)' }}
          accessibilityRole="button"
          accessibilityLabel="실시간 분석 시작"
          style={({ pressed }) => ({ opacity: pressed ? 0.92 : 1 })}
        >
          <Card style={{ gap: 16 }}>
            <View style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
              <IconCircle name="mic" color={colors.primary} bg={colors.primaryFaint} size={48} radius={24} iconSize={22} />
              <View style={{ flex: 1 }}>
                <AppText weight="800" style={{ fontSize: 17, lineHeight: 24 }}>
                  실시간 통화 분석
                </AppText>
                <AppText color={colors.textSecondary} style={[{ fontSize: 13, lineHeight: 18, marginTop: 2 }, keepAll]}>
                  통화 중 보이스피싱 위험을 바로 알려드려요
                </AppText>
              </View>
            </View>
            <View
              style={{
                height: 52,
                borderRadius: 12,
                backgroundColor: brandFill,
                flexDirection: 'row',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 8,
              }}
            >
              <Icon name="activity" size={18} color={colors.onPrimary} />
              <AppText weight="700" color={colors.onPrimary} style={{ fontSize: 16 }}>
                실시간 분석 시작
              </AppText>
            </View>
          </Card>
        </Pressable>

        {/* 받은 문자·문서·녹음을 직접 확인하는 도구 (보조 진입: 채우지 않은 한 카드 목록) */}
        <Card padding={0}>
          <ToolRow
            icon={<ChatGlyph size={20} color={colors.primary} />}
            title="문자 검사"
            desc="받은 문자가 사기인지 확인해요"
            divider={false}
            onPress={() => router.push('/message')}
          />
          <ToolRow
            icon={<Icon name="file" size={20} color={colors.primary} />}
            title="문서 점검"
            desc="공문·영장 사진이 진짜인지 확인해요"
            divider
            onPress={() => router.push('/document')}
          />
          <ToolRow
            icon={<Icon name="upload" size={20} color={colors.primary} />}
            title="음성 파일 분석"
            desc="통화 녹음 파일을 확인해요"
            divider
            onPress={() => router.push('/upload')}
          />
        </Card>

        <View
          style={{
            flexDirection: 'row',
            alignItems: 'center',
            justifyContent: 'space-between',
            marginTop: 12,
            paddingLeft: 8,
          }}
        >
          <AppText weight="800" style={{ fontSize: 17, lineHeight: 24 }}>
            최근 분석 내역
          </AppText>
          <Pressable
            onPress={() => router.push('/history')}
            accessibilityRole="button"
            style={{ minHeight: 44, paddingHorizontal: 8, flexDirection: 'row', alignItems: 'center', gap: 2 }}
          >
            <AppText weight="600" color={colors.primary} style={{ fontSize: 14 }}>
              전체 보기
            </AppText>
            <Icon name="chevron-right" size={16} color={colors.primary} />
          </Pressable>
        </View>

        <Card padding={0} style={{ marginTop: -8 }}>
          {pending ? (
            <View accessible accessibilityLabel="불러오는 중">
              {[0, 1, 2].map((i) => (
                <SkeletonRow key={i} divider={i > 0} />
              ))}
            </View>
          ) : unavailable ? (
            <View style={{ flexDirection: 'row', alignItems: 'center', gap: 12, paddingHorizontal: 16, paddingVertical: 20 }}>
              <IconCircle
                name="alert-triangle"
                color={colors.warningText}
                bg={colors.warningFaint}
                size={40}
                radius={20}
                iconSize={20}
              />
              <AppText style={{ fontSize: 14, lineHeight: 20, flex: 1 }}>최근 내역을 불러오지 못했어요.</AppText>
            </View>
          ) : recent.length === 0 ? (
            <View style={{ alignItems: 'center', gap: 8, paddingHorizontal: 16, paddingVertical: 24 }}>
              <IconCircle name="clock" color={colors.textMuted} bg={colors.cardAlt} size={40} radius={20} iconSize={20} />
              <AppText color={colors.textSecondary} style={{ fontSize: 14, lineHeight: 20, textAlign: 'center' }}>
                아직 분석한 통화가 없어요.
              </AppText>
            </View>
          ) : (
            recent.map((c, i) => (
              <RecentRow
                key={c.id}
                category={c.phishing_type || '분석 결과'}
                score={c.risk_score}
                createdAt={c.called_at}
                divider={i > 0}
                onPress={() => router.push(`/result?id=${c.id}`)}
              />
            ))
          )}
        </Card>

        {/* 수사 지원 대시보드: 발표용 넓은 화면에서만, 눈에 띄지 않는 바닥 링크로 */}
        {showDashboardLink ? (
          <Pressable
            onPress={() => void Linking.openURL(`${BASE_URL_HTTP}/dashboard`)}
            accessibilityRole="link"
            style={{ alignSelf: 'center', minHeight: 44, paddingHorizontal: 12, flexDirection: 'row', alignItems: 'center', gap: 6 }}
          >
            <Icon name="grid" size={14} color={colors.textSecondary} />
            <AppText weight="600" color={colors.textSecondary} style={{ fontSize: 13 }}>
              수사 지원 대시보드
            </AppText>
            <Icon name="chevron-right" size={14} color={colors.textSecondary} />
          </Pressable>
        ) : null}
      </ScrollView>
    </View>
  );
}
