/**
 * 문자 검사: 받은 문자·메신저 메시지를 붙여넣거나 캡처를 올리면 스미싱 여부를 알려준다.
 */
import { useState } from 'react';
import { Pressable, ScrollView, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import * as DocumentPicker from 'expo-document-picker';
import { AppText, keepAll } from '@/components/AppText';
import { Button } from '@/components/Button';
import { CheckResult, CheckSection } from '@/components/CheckResult';
import { Icon } from '@/components/Icon';
import { ScreenHeader } from '@/components/ScreenHeader';
import { useTheme } from '@/core/theme/theme';
import { friendlyNetworkError } from '@/core/utils/format';
import { checkMessage, type MessageCheck, type PickedImage } from '@/data/services/analyzeApi';

const EXAMPLE = '[국제발신] 고객님 택배가 주소 불일치로 반송 예정입니다. 주소 수정: han.gl/AbC12';
const URL_VERDICT = { suspicious: '의심', unknown: '확인 필요', likely_official: '공식 주소로 보임' } as const;

export default function MessageCheckScreen() {
  const { colors, fontScale } = useTheme();
  const [text, setText] = useState('');
  const [image, setImage] = useState<PickedImage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<MessageCheck | null>(null);

  const pickImage = async () => {
    const res = await DocumentPicker.getDocumentAsync({ type: 'image/*', copyToCacheDirectory: true });
    if (!res.canceled && res.assets?.[0]) {
      const a = res.assets[0];
      setImage({ uri: a.uri, name: a.name, mime: a.mimeType ?? undefined });
      setResult(null);
    }
  };

  const run = async () => {
    setLoading(true);
    setError('');
    setResult(null);
    try {
      setResult(await checkMessage(text.trim(), image));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  const canRun = text.trim().length > 0 || image !== null;
  const urlColor = { suspicious: colors.dangerText, unknown: colors.warningText, likely_official: colors.safeText };

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }} edges={['top', 'bottom']}>
      <ScreenHeader title="문자 검사" />
      <ScrollView
        contentContainerStyle={{ paddingHorizontal: 20, paddingTop: 4, paddingBottom: 24, gap: 16 }}
        keyboardShouldPersistTaps="handled"
      >
        <AppText color={colors.textSecondary} style={{ fontSize: 15, lineHeight: 23 }}>
          받은 문자나 카톡 내용을 붙여넣으면 사기 문자(스미싱)인지 확인해 드려요.{'\n'}링크는 누르지 말고 내용만
          복사하세요.
        </AppText>

        <View style={{ gap: 8 }}>
          <TextInput
            value={text}
            onChangeText={(value) => {
              setText(value);
              setResult(null);
            }}
            placeholder="여기에 메시지 내용을 붙여넣으세요"
            placeholderTextColor={colors.textMuted}
            multiline
            accessibilityLabel="메시지 내용"
            style={[
              {
                minHeight: 160,
                borderWidth: 1,
                borderColor: colors.border,
                borderRadius: 16,
                backgroundColor: colors.card,
                color: colors.text,
                padding: 16,
                fontSize: 15 * fontScale,
                lineHeight: 22 * fontScale,
                textAlignVertical: 'top',
              },
              keepAll,
            ]}
          />

          {/* 실제 입력 수단(캡처)은 테두리 버튼, 시연용 예시는 글자 버튼으로 낮춰 '검사하기'만 채운 버튼으로 남긴다 */}
          <View style={{ flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' }}>
            <Button
              title={image ? '캡처 바꾸기' : '캡처 올리기'}
              variant="outlined"
              icon="upload"
              onPress={pickImage}
              style={{ minHeight: 44, borderRadius: 12, paddingHorizontal: 16 }}
            />
            <Pressable
              onPress={() => setText(EXAMPLE)}
              accessibilityRole="button"
              style={({ pressed }) => ({
                minHeight: 44,
                paddingHorizontal: 8,
                justifyContent: 'center',
                opacity: pressed ? 0.6 : 1,
              })}
            >
              <AppText weight="600" color={colors.textSecondary} style={{ fontSize: 14 }}>
                예시 넣기
              </AppText>
            </Pressable>
          </View>
          {image ? (
            <AppText color={colors.textSecondary} style={{ fontSize: 13, lineHeight: 18 }}>
              첨부한 캡처: {image.name}
            </AppText>
          ) : null}
        </View>

        {error ? (
          <View
            accessibilityRole="alert"
            style={{ flexDirection: 'row', gap: 8, padding: 12, borderRadius: 12, backgroundColor: colors.dangerFaint }}
          >
            <Icon name="alert-triangle" size={18} color={colors.dangerText} />
            <AppText color={colors.dangerText} style={{ fontSize: 14, lineHeight: 20, flex: 1 }}>
              {friendlyNetworkError(error)}
            </AppText>
          </View>
        ) : null}

        {result ? (
          <CheckResult
            riskScore={result.risk_score}
            riskLevel={result.risk_level}
            title={result.category_label}
            flags={result.red_flags}
            explanation={result.explanation}
            actions={result.recommended_actions}
          >
            {result.urls.length > 0 ? (
              <CheckSection title="메시지 속 링크">
                {result.urls.map((item, i) => (
                  <View key={item.url} style={{ gap: 4 }}>
                    {i > 0 ? <View style={{ height: 1, backgroundColor: colors.border, marginBottom: 8 }} /> : null}
                    <View style={{ flexDirection: 'row', alignItems: 'baseline', justifyContent: 'space-between', gap: 12 }}>
                      <AppText weight="700" style={{ fontSize: 14, lineHeight: 20, flex: 1 }} numberOfLines={1}>
                        {item.url}
                      </AppText>
                      <AppText weight="800" color={urlColor[item.verdict]} style={{ fontSize: 13, lineHeight: 18 }}>
                        {URL_VERDICT[item.verdict]}
                      </AppText>
                    </View>
                    {item.reasons.map((reason) => (
                      <AppText key={reason} color={colors.textSecondary} style={{ fontSize: 13, lineHeight: 19 }}>
                        · {reason}
                      </AppText>
                    ))}
                  </View>
                ))}
              </CheckSection>
            ) : null}
            {result.extracted_text ? (
              <CheckSection title="캡처에서 읽은 내용">
                <AppText style={{ fontSize: 14, lineHeight: 21 }}>{result.extracted_text}</AppText>
              </CheckSection>
            ) : null}
          </CheckResult>
        ) : null}
      </ScrollView>

      {/* 하단 고정: 이 화면의 유일한 채운 버튼. 왜 눌리지 않는지, 지금 무엇을 하는지 바로 위에 알려 준다. */}
      <View
        style={{
          paddingHorizontal: 20,
          paddingTop: 12,
          paddingBottom: 20,
          gap: 8,
          borderTopWidth: 1,
          borderTopColor: colors.border,
          backgroundColor: colors.bg,
        }}
      >
        {loading || !canRun ? (
          <AppText
            aria-live="polite"
            color={colors.textSecondary}
            style={{ fontSize: 14, lineHeight: 20, textAlign: 'center' }}
          >
            {loading
              ? '문자를 확인하고 있어요. 10초 정도 걸릴 수 있어요.'
              : '메시지를 붙여넣거나 캡처를 올리면 검사할 수 있어요.'}
          </AppText>
        ) : null}
        <Button title="검사하기" onPress={run} loading={loading} disabled={!canRun} style={{ minHeight: 56 }} />
      </View>
    </SafeAreaView>
  );
}
