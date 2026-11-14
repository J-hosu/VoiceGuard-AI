/**
 * 문자 검사: 받은 문자·메신저 메시지를 붙여넣거나 캡처를 올리면 스미싱 여부를 알려준다.
 */
import { useState } from 'react';
import { ScrollView, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import * as DocumentPicker from 'expo-document-picker';
import { AppText } from '@/components/AppText';
import { Button } from '@/components/Button';
import { CheckResult } from '@/components/CheckResult';
import { ScreenHeader } from '@/components/ScreenHeader';
import { useTheme } from '@/core/theme/theme';
import { checkMessage, type MessageCheck, type PickedImage } from '@/data/services/analyzeApi';

const EXAMPLE = '[국제발신] 고객님 택배가 주소 불일치로 반송 예정입니다. 주소 수정: han.gl/AbC12';
const URL_VERDICT = { suspicious: '의심', unknown: '확인 필요', likely_official: '공식 주소로 보임' } as const;

export default function MessageCheckScreen() {
  const { colors } = useTheme();
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

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }} edges={['top', 'bottom']}>
      <ScreenHeader title="문자 검사" />
      <ScrollView contentContainerStyle={{ padding: 20, gap: 16 }} keyboardShouldPersistTaps="handled">
        <AppText color={colors.textSecondary} style={{ fontSize: 14, lineHeight: 22 }}>
          받은 문자나 메신저 메시지를 붙여넣으면 스미싱인지 확인해 드려요. 링크는 누르지 말고 내용만 복사하세요.
        </AppText>

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
          style={{
            minHeight: 130,
            borderWidth: 1,
            borderColor: colors.border,
            borderRadius: 16,
            backgroundColor: colors.card,
            color: colors.text,
            padding: 14,
            fontSize: 15,
            lineHeight: 22,
            textAlignVertical: 'top',
          }}
        />

        <View style={{ flexDirection: 'row', gap: 10 }}>
          <Button
            title={image ? '캡처 바꾸기' : '캡처 올리기'}
            variant="outlined"
            icon="upload"
            onPress={pickImage}
            style={{ flex: 1 }}
          />
          <Button title="예시 넣기" variant="outlined" onPress={() => setText(EXAMPLE)} style={{ flex: 1 }} />
        </View>
        {image ? (
          <AppText color={colors.textMuted} style={{ fontSize: 12.5 }}>
            첨부한 캡처: {image.name}
          </AppText>
        ) : null}

        <Button title="검사하기" onPress={run} loading={loading} disabled={!canRun || loading} />

        {error ? (
          <AppText color={colors.danger} style={{ fontSize: 13.5 }}>
            {error}
          </AppText>
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
            {result.extracted_text ? (
              <View style={{ backgroundColor: colors.cardAlt, borderRadius: 14, padding: 14, gap: 6 }}>
                <AppText weight="700" color={colors.textMuted} style={{ fontSize: 12 }}>
                  캡처에서 읽은 내용
                </AppText>
                <AppText color={colors.text} style={{ fontSize: 13.5, lineHeight: 20 }}>
                  {result.extracted_text}
                </AppText>
              </View>
            ) : null}
            {result.urls.length > 0 ? (
              <View style={{ gap: 8 }}>
                <AppText weight="800" style={{ fontSize: 15 }}>
                  메시지 속 링크
                </AppText>
                {result.urls.map((item) => (
                  <View
                    key={item.url}
                    style={{ borderWidth: 1, borderColor: colors.border, borderRadius: 14, padding: 12, gap: 4 }}
                  >
                    <View style={{ flexDirection: 'row', justifyContent: 'space-between', gap: 10 }}>
                      <AppText weight="700" style={{ fontSize: 13.5, flex: 1 }} numberOfLines={1}>
                        {item.url}
                      </AppText>
                      <AppText
                        weight="800"
                        color={item.verdict === 'suspicious' ? colors.danger : item.verdict === 'likely_official' ? colors.safe : colors.textMuted}
                        style={{ fontSize: 12.5 }}
                      >
                        {URL_VERDICT[item.verdict]}
                      </AppText>
                    </View>
                    {item.reasons.map((reason) => (
                      <AppText key={reason} color={colors.textSecondary} style={{ fontSize: 12.5, lineHeight: 18 }}>
                        · {reason}
                      </AppText>
                    ))}
                  </View>
                ))}
              </View>
            ) : null}
          </CheckResult>
        ) : null}
      </ScrollView>
    </SafeAreaView>
  );
}
