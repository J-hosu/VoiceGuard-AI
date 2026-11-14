import { Pressable, ScrollView, StyleSheet, View } from 'react-native';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { AppText, balanced } from '@/components/AppText';
import { Button } from '@/components/Button';
import { Icon } from '@/components/Icon';
import { evidenceBullets, recommendedAction } from '@/data/mock/infoContent';

const RED = '#DC2626';

/** 전면 위험 경고 (강한 경고 임계값 도달 시 자동 표시). 진동/햅틱은 호출 측에서 처리. */
export default function Warning() {
  const router = useRouter();
  const params = useLocalSearchParams<{ score?: string; patterns?: string; category?: string }>();
  const patterns = (params.patterns ?? '').split('|').filter(Boolean);
  // 근거는 한눈에 읽히는 만큼만(최대 3개) 작은 태그로 보여준다. 전체 근거는 결과 화면에서 확인한다.
  const bullets = evidenceBullets(patterns).slice(0, 3);
  const advice = recommendedAction(params.category ?? '');
  // 위험도 숫자는 싣지 않는다. 팝업을 띄운 순간의 점수와 돌아간 통화 화면의 점수가 달라 보이면
  // "괜찮아졌다"로 읽힐 수 있어, 숫자는 통화 화면의 위험도 카드 한 곳에서만 보여준다.

  // 경고는 항상 /realtime에서 push되어 열린다. canGoBack()이 어떤 이유로든 false면
  // (예: 새로고침으로 히스토리가 끊긴 경우) 홈으로 보내면 진행 중이던 실시간 세션이
  // 그대로 끊겨버리므로, 홈 대신 realtime으로 돌아가게 한다.
  const close = () => (router.canGoBack() ? router.back() : router.replace('/realtime'));
  const goResponse = () => {
    close();
    setTimeout(() => router.push('/prevention'), 60);
  };

  return (
    <View
      style={{
        flex: 1,
        backgroundColor: 'rgba(0,0,0,0.6)',
        justifyContent: 'center',
        paddingHorizontal: 16,
        paddingVertical: 24,
      }}
    >
      <StatusBar style="light" />
      {/* 배경(뒤 화면 비침) 탭 시 닫기. 키보드·화면낭독기 순서에서는 빼고 카드의 버튼으로 닫게 한다. */}
      <Pressable
        style={StyleSheet.absoluteFill}
        onPress={close}
        accessibilityLabel="경고 닫기"
        aria-hidden
        focusable={false}
        tabIndex={-1}
      />

      {/* 중앙 팝업 카드: 제목 → 지금 할 일 → 버튼 순으로만 읽히게 한다. 화면이 낮으면 카드 안에서 스크롤된다. */}
      <View
        role="alert"
        aria-live="assertive"
        aria-modal
        style={{
          width: '100%',
          maxHeight: '100%',
          backgroundColor: RED,
          borderRadius: 24,
          shadowColor: '#000',
          shadowOpacity: 0.4,
          shadowRadius: 24,
          shadowOffset: { width: 0, height: 12 },
          elevation: 12,
        }}
      >
        <ScrollView
          bounces={false}
          style={{ flexGrow: 0 }}
          contentContainerStyle={{ alignItems: 'center', paddingHorizontal: 24, paddingTop: 32, paddingBottom: 24 }}
          showsVerticalScrollIndicator={false}
        >
          <View
            style={{
              width: 72,
              height: 72,
              borderRadius: 36,
              backgroundColor: 'rgba(255,255,255,0.18)',
              alignItems: 'center',
              justifyContent: 'center',
            }}
          >
            <Icon name="alert-triangle" size={36} color="#FFFFFF" />
          </View>

          <AppText
            weight="800"
            color="#FFFFFF"
            style={[{ fontSize: 30, lineHeight: 40, textAlign: 'center', marginTop: 16 }, balanced]}
          >
            보이스피싱{'\n'}위험이 높아요
          </AppText>

          {/* 지금 해야 할 일: 제목 다음으로 가장 크게 */}
          <AppText
            weight="700"
            color="#FFFFFF"
            style={[{ fontSize: 17, lineHeight: 26, textAlign: 'center', marginTop: 16 }, balanced]}
          >
            {advice}
          </AppText>

          {/* 탐지 근거: 보조 정보라 작은 태그로 */}
          <View style={{ flexDirection: 'row', flexWrap: 'wrap', justifyContent: 'center', gap: 8, marginTop: 16 }}>
            {bullets.map((b, i) => (
              <View
                key={i}
                style={{
                  paddingHorizontal: 12,
                  paddingVertical: 6,
                  borderRadius: 999,
                  backgroundColor: 'rgba(0,0,0,0.2)',
                }}
              >
                <AppText weight="600" color="#FFFFFF" style={{ fontSize: 13, lineHeight: 18 }}>
                  {b}
                </AppText>
              </View>
            ))}
          </View>

          <View style={{ width: '100%', gap: 8, marginTop: 24 }}>
            <Button
              title="대처 방법 보기"
              variant="white"
              textColor={RED}
              onPress={goResponse}
              style={{ minHeight: 56, borderRadius: 16 }}
            />
            {/* 빨강 위에서 흰 글씨 대비를 지키려고 밝게 비치는 면 대신 한 단계 어두운 면을 쓴다 */}
            <Pressable
              onPress={close}
              accessibilityRole="button"
              style={({ pressed }) => ({
                minHeight: 48,
                borderRadius: 16,
                alignItems: 'center',
                justifyContent: 'center',
                backgroundColor: pressed ? 'rgba(0,0,0,0.32)' : 'rgba(0,0,0,0.22)',
              })}
            >
              <AppText weight="700" color="#FFFFFF" style={{ fontSize: 15 }}>
                계속 지켜보기 (닫기)
              </AppText>
            </Pressable>
          </View>
        </ScrollView>
      </View>
    </View>
  );
}
