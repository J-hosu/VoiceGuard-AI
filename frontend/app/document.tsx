/**
 * 문서 점검: 받은 공문·영장·대출 서류 등의 사진을 올리면 피싱용 위조 문서의 특징이 있는지 알려준다.
 */
import { useState } from 'react';
import { Image, ScrollView, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import * as DocumentPicker from 'expo-document-picker';
import { AppText } from '@/components/AppText';
import { Button } from '@/components/Button';
import { CheckResult } from '@/components/CheckResult';
import { ScreenHeader } from '@/components/ScreenHeader';
import { useTheme } from '@/core/theme/theme';
import { checkDocument, type DocumentCheck, type PickedImage } from '@/data/services/analyzeApi';

export default function DocumentCheckScreen() {
  const { colors } = useTheme();
  const [image, setImage] = useState<PickedImage | null>(null);
  const [note, setNote] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<DocumentCheck | null>(null);

  const pickImage = async () => {
    const res = await DocumentPicker.getDocumentAsync({ type: 'image/*', copyToCacheDirectory: true });
    if (!res.canceled && res.assets?.[0]) {
      const a = res.assets[0];
      setImage({ uri: a.uri, name: a.name, mime: a.mimeType ?? undefined });
      setResult(null);
      setError('');
    }
  };

  const run = async () => {
    if (!image) return;
    setLoading(true);
    setError('');
    setResult(null);
    try {
      setResult(await checkDocument(image, note));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }} edges={['top', 'bottom']}>
      <ScreenHeader title="문서 점검" />
      <ScrollView contentContainerStyle={{ padding: 20, gap: 16 }} keyboardShouldPersistTaps="handled">
        <AppText color={colors.textSecondary} style={{ fontSize: 14, lineHeight: 22 }}>
          검찰·경찰·금융감독원 공문, 구속영장, 대출 승인서처럼 누군가 보낸 문서의 사진이나 캡처를 올리면 보이스피싱에
          쓰이는 위조 문서의 특징이 있는지 확인해 드려요.
        </AppText>

        {image ? (
          <Image
            source={{ uri: image.uri }}
            resizeMode="contain"
            accessibilityLabel="선택한 문서 이미지"
            style={{ width: '100%', height: 260, borderRadius: 16, backgroundColor: colors.cardAlt }}
          />
        ) : null}

        <Button title={image ? '다른 사진 고르기' : '문서 사진 고르기'} variant="outlined" icon="upload" onPress={pickImage} />

        <TextInput
          value={note}
          onChangeText={setNote}
          placeholder="(선택) 어떻게 받은 문서인지 적어 주세요. 예: 검사라는 사람이 카톡으로 보냄"
          placeholderTextColor={colors.textMuted}
          multiline
          accessibilityLabel="상황 설명"
          style={{
            minHeight: 64,
            borderWidth: 1,
            borderColor: colors.border,
            borderRadius: 16,
            backgroundColor: colors.card,
            color: colors.text,
            padding: 14,
            fontSize: 14.5,
            lineHeight: 21,
            textAlignVertical: 'top',
          }}
        />

        <Button title="점검하기" onPress={run} loading={loading} disabled={!image || loading} />
        {loading ? (
          <AppText color={colors.textMuted} style={{ fontSize: 12.5 }}>
            문서를 읽고 있어요. 10~20초 정도 걸릴 수 있습니다.
          </AppText>
        ) : null}

        {error ? (
          <AppText color={colors.danger} style={{ fontSize: 13.5 }}>
            {error}
          </AppText>
        ) : null}

        {result && !result.readable ? (
          <AppText color={colors.textSecondary} style={{ fontSize: 14, lineHeight: 22 }}>
            문서 내용을 읽지 못했어요. 글자가 또렷하게 보이도록 다시 찍어서 올려 주세요.
          </AppText>
        ) : null}

        {result && result.readable ? (
          <CheckResult
            riskScore={result.risk_score}
            riskLevel={result.risk_level}
            title={[result.doc_type, result.claimed_issuer].filter(Boolean).join(' · ') || '문서'}
            flags={result.flags}
            explanation={result.explanation}
            actions={result.recommended_actions}
          >
            {result.evidence.length > 0 ? (
              <View style={{ backgroundColor: colors.cardAlt, borderRadius: 14, padding: 14, gap: 6 }}>
                <AppText weight="700" color={colors.textMuted} style={{ fontSize: 12 }}>
                  문서에서 확인한 내용
                </AppText>
                {result.evidence.map((item) => (
                  <AppText key={item} color={colors.text} style={{ fontSize: 13.5, lineHeight: 20 }}>
                    · {item}
                  </AppText>
                ))}
              </View>
            ) : null}
            <AppText color={colors.textMuted} style={{ fontSize: 12.5, lineHeight: 19 }}>
              {result.disclaimer}
            </AppText>
          </CheckResult>
        ) : null}
      </ScrollView>
    </SafeAreaView>
  );
}
