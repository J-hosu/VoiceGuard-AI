import { useEffect } from 'react';
import { Platform, View, useWindowDimensions } from 'react-native';
import { GestureHandlerRootView } from 'react-native-gesture-handler';
import { SafeAreaProvider } from 'react-native-safe-area-context';
import { Stack } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { DemoPanel } from '@/components/DemoPanel';
import { useTheme } from '@/core/theme/theme';
import { useAppStore } from '@/state/appStore';
import { useCallStore } from '@/state/callStore';
import { useSettingsStore } from '@/state/settingsStore';

export default function RootLayout() {
  const hydrateSettings = useSettingsStore((s) => s.hydrate);
  const hydrateApp = useAppStore((s) => s.hydrate);
  const fetchCalls = useCallStore((s) => s.fetchCalls);
  const { colors, isDark } = useTheme();
  // 발표용: 화면이 충분히 넓으면(노트북·프로젝터) 앱 옆에 AI 분석 과정 패널을 함께 보여준다.
  const { width } = useWindowDimensions();
  const showDemoPanel = Platform.OS === 'web' && width >= 1180;

  useEffect(() => {
    void hydrateSettings();
    void hydrateApp();
    void fetchCalls(); // 히스토리는 백엔드에서 조회
  }, [hydrateSettings, hydrateApp, fetchCalls]);

  const stack = (
    <Stack
      screenOptions={{
        headerShown: false,
        contentStyle: { backgroundColor: colors.bg },
        animation: 'slide_from_right',
      }}
    >
      <Stack.Screen name="onboarding" />
      <Stack.Screen name="(tabs)" />
      <Stack.Screen name="upload" />
      <Stack.Screen name="realtime" options={{ animation: 'slide_from_bottom' }} />
      <Stack.Screen name="result" />
      <Stack.Screen name="prevention" />
      <Stack.Screen name="message" />
      <Stack.Screen name="document" />
      <Stack.Screen name="settings" />
      {/* 경고 팝업 뒤로 진행 중인 통화 화면이 비쳐 보이도록 배경을 투명하게 둔다(어둡게 덮는 막은 warning.tsx가 그린다) */}
      <Stack.Screen
        name="warning"
        options={{ presentation: 'transparentModal', animation: 'fade', contentStyle: { backgroundColor: 'transparent' } }}
      />
    </Stack>
  );

  return (
    <GestureHandlerRootView style={{ flex: 1 }}>
      <SafeAreaProvider>
        <StatusBar style={isDark ? 'light' : 'dark'} />
        {Platform.OS === 'web' ? (
          // 웹(데스크톱)에서는 폰처럼 가운데 모바일 폭으로 표시
          <View
            style={{
              flex: 1,
              flexDirection: 'row',
              justifyContent: 'center',
              backgroundColor: '#0C0C0E',
              gap: showDemoPanel ? 20 : 0,
              padding: showDemoPanel ? 20 : 0,
            }}
          >
            <View
              style={{
                flex: showDemoPanel ? undefined : 1,
                width: showDemoPanel ? 420 : '100%',
                maxWidth: 440,
                backgroundColor: colors.bg,
                borderRadius: showDemoPanel ? 28 : 0,
                // 어두운 화면에서도 휴대폰 틀의 경계가 보이도록 옅은 테두리를 둔다.
                borderWidth: showDemoPanel ? 1 : 0,
                borderColor: 'rgba(255,255,255,0.08)',
                overflow: 'hidden',
                shadowColor: '#000',
                shadowOpacity: 0.3,
                shadowRadius: 24,
              }}
            >
              {stack}
            </View>
            {showDemoPanel ? (
              <View style={{ flex: 1, maxWidth: 980, borderRadius: 28, overflow: 'hidden' }}>
                <DemoPanel />
              </View>
            ) : null}
          </View>
        ) : (
          stack
        )}
      </SafeAreaProvider>
    </GestureHandlerRootView>
  );
}
