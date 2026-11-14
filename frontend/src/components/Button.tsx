import { ActivityIndicator, Pressable, View, type ViewStyle } from 'react-native';
import { AppText } from './AppText';
import { Icon, type IconName } from './Icon';
import { useTheme } from '@/core/theme/theme';

type Variant = 'filled' | 'outlined' | 'white';

/** Material3 스타일 버튼. 최소 터치 높이 52dp (접근성). */
export function Button({
  title,
  onPress,
  variant = 'filled',
  color,
  textColor,
  icon,
  loading = false,
  disabled = false,
  style,
}: {
  title: string;
  onPress?: () => void;
  variant?: Variant;
  color?: string;
  textColor?: string;
  icon?: IconName;
  loading?: boolean;
  disabled?: boolean;
  style?: ViewStyle;
}) {
  const { colors, isDark } = useTheme();
  const primary = color ?? colors.primary;
  // 다크 모드의 밝은 파랑(#3B82F6) 위 흰 글씨는 대비가 낮아, 채운 면에는 한 단계 진한 파랑을 쓴다.
  const fill = color ?? (isDark ? colors.primaryDark : colors.primary);
  const inactive = disabled || loading;

  let bg = fill;
  let fg = textColor ?? colors.onPrimary;
  let border = 'transparent';
  if (variant === 'outlined') {
    bg = 'transparent';
    fg = textColor ?? primary;
    border = primary;
  } else if (variant === 'white') {
    bg = '#FFFFFF';
    fg = textColor ?? fill;
  }

  return (
    <Pressable
      onPress={inactive ? undefined : onPress}
      disabled={inactive}
      android_ripple={{ color: 'rgba(0,0,0,0.08)' }}
      style={({ pressed }) => [
        {
          minHeight: 52,
          borderRadius: 16,
          backgroundColor: bg,
          borderWidth: variant === 'outlined' ? 1.5 : 0,
          borderColor: border,
          alignItems: 'center',
          justifyContent: 'center',
          flexDirection: 'row',
          gap: 8,
          paddingHorizontal: 20,
          opacity: disabled ? 0.5 : pressed ? 0.85 : 1,
        },
        style,
      ]}
      accessibilityRole="button"
      accessibilityLabel={title}
      accessibilityState={{ disabled: inactive, busy: loading }}
    >
      {/* 처리 중에도 제목을 남겨 무엇을 하는 중인지 알 수 있게 한다 */}
      {loading ? <ActivityIndicator color={fg} /> : icon ? <Icon name={icon} size={20} color={fg} /> : null}
      <AppText weight="700" color={fg} style={{ fontSize: 16 }}>
        {title}
      </AppText>
    </Pressable>
  );
}
