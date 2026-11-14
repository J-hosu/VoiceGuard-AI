import { type ReactNode } from 'react';
import { Pressable, View } from 'react-native';
import { useRouter } from 'expo-router';
import { AppText } from './AppText';
import { Icon } from './Icon';
import { useTheme } from '@/core/theme/theme';

/** 뒤로가기 + 제목 헤더 (push된 화면용) */
export function ScreenHeader({
  title,
  right,
  color,
  onBack,
}: {
  title: string;
  right?: ReactNode;
  color?: string;
  onBack?: () => void;
}) {
  const { colors } = useTheme();
  const router = useRouter();
  const fg = color ?? colors.text;

  return (
    <View
      style={{
        flexDirection: 'row',
        alignItems: 'center',
        paddingHorizontal: 18,
        paddingVertical: 12,
        gap: 10,
      }}
    >
      {/* 터치 영역은 44x44로 키우되(웹은 hitSlop을 무시한다) 음수 여백으로 아이콘 자리(26)만 차지하게 한다 */}
      <Pressable
        onPress={onBack ?? (() => (router.canGoBack() ? router.back() : router.replace('/')))}
        accessibilityRole="button"
        accessibilityLabel="뒤로"
        style={{ width: 44, height: 44, margin: -9, alignItems: 'center', justifyContent: 'center' }}
      >
        <Icon name="chevron-left" size={26} color={fg} />
      </Pressable>
      <AppText weight="800" color={fg} style={{ fontSize: 18, flex: 1 }} numberOfLines={1}>
        {title}
      </AppText>
      {right ?? null}
    </View>
  );
}
