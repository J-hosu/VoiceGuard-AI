import { Pressable, View } from 'react-native';
import { AppText } from './AppText';
import { Card } from './Card';
import { IconCircle } from './IconCircle';
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
import { useSettingsStore } from '@/state/settingsStore';

/** 최근내역/히스토리 공용 카드 (좌측 색 아이콘 + 유형/시각 + 우측 위험도) */
export function CallCard({
  category,
  score,
  source,
  createdAt,
  onPress,
  showMeta = true,
  unheard = false,
}: {
  category: string;
  score: number; // 0..1
  source: 'file' | 'realtime';
  createdAt: string; // ISO
  onPress?: () => void;
  showMeta?: boolean; // true면 "날짜 · 소스"와 등급 라벨 표시(히스토리)
  unheard?: boolean; // 받아 적은 말이 없어 위험도를 매기지 않은 통화: '정상'·'0%' 대신 판단 없음으로 보인다
}) {
  const { colors } = useTheme();
  const dangerThreshold = useSettingsStore((s) => s.dangerThreshold);
  const level = riskLevelFromScore(score, dangerThreshold);
  const main = unheard ? colors.textMuted : riskColor(level, colors);
  const mainText = unheard ? colors.textMuted : riskColorText(level, colors);
  const sourceText = source === 'file' ? '파일' : '실시간';

  return (
    <Pressable onPress={onPress} android_ripple={{ color: 'rgba(0,0,0,0.05)' }}>
      <Card padding={14} style={{ flexDirection: 'row', alignItems: 'center', gap: 13 }}>
        <IconCircle
          name={unheard ? 'volume-x' : level === 'safe' ? 'shield-check' : 'shield-alert'}
          color={main}
          bg={unheard ? colors.cardAlt : riskColorFaint(level, colors)}
          size={40}
          radius={20}
          iconSize={20}
        />
        <View style={{ flex: 1 }}>
          <AppText weight="700" style={{ fontSize: 15 }} numberOfLines={1}>
            {unheard ? '들은 말 없음' : category}
          </AppText>
          <AppText color={colors.textMuted} style={{ fontSize: 12, marginTop: 2 }}>
            {formatRelativeDate(createdAt)}
            {showMeta ? ` · ${sourceText}` : ''}
          </AppText>
        </View>
        <View style={{ alignItems: 'flex-end' }}>
          <AppText weight="800" color={mainText} style={{ fontSize: 18 }}>
            {unheard ? '–' : `${toPercent(score)}%`}
          </AppText>
          {showMeta ? (
            <AppText weight="600" color={mainText} style={{ fontSize: 12, marginTop: 1 }}>
              {unheard ? '판단 없음' : riskLabel(level)}
            </AppText>
          ) : null}
        </View>
      </Card>
    </Pressable>
  );
}
