import { ActivityIndicator, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { Redirect, Tabs } from 'expo-router';
import { Icon } from '@/components/Icon';
import { useTheme } from '@/core/theme/theme';
import { useAppStore } from '@/state/appStore';

export default function TabsLayout() {
  const { colors } = useTheme();
  // 높이를 직접 정하면 라이브러리가 하단 안전 영역(홈 인디케이터)을 더하지 않으므로 직접 더한다.
  const insets = useSafeAreaInsets();
  const onboarded = useAppStore((s) => s.onboarded);

  if (onboarded === null) {
    return (
      <View style={{ flex: 1, alignItems: 'center', justifyContent: 'center', backgroundColor: colors.bg }}>
        <ActivityIndicator color={colors.primary} />
      </View>
    );
  }
  if (!onboarded) {
    return <Redirect href="/onboarding" />;
  }

  return (
    <Tabs
      screenOptions={{
        headerShown: false,
        tabBarActiveTintColor: colors.primary,
        tabBarInactiveTintColor: colors.textSecondary,
        // 탭 항목은 위아래 여백 5 + 아이콘 28 + 라벨(16)이 필요하다. 64로 누르면 라벨이 잘린다.
        tabBarStyle: {
          backgroundColor: colors.card,
          borderTopColor: colors.border,
          height: 72 + insets.bottom,
          paddingTop: 6,
          paddingBottom: 8 + insets.bottom,
        } as never,
        tabBarLabelStyle: { fontSize: 12, lineHeight: 16, fontWeight: '600' },
      }}
    >
      <Tabs.Screen
        name="index"
        options={{
          title: '홈',
          tabBarIcon: ({ color }) => <Icon name="home" size={22} color={color as string} />,
        }}
      />
      <Tabs.Screen
        name="history"
        options={{
          title: '히스토리',
          tabBarIcon: ({ color }) => <Icon name="clock" size={22} color={color as string} />,
        }}
      />
      <Tabs.Screen
        name="info"
        options={{
          title: '정보',
          tabBarIcon: ({ color }) => <Icon name="info" size={22} color={color as string} />,
        }}
      />
    </Tabs>
  );
}
