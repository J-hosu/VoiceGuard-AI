/**
 * 문서 점검: 받은 공문·영장·대출 서류 등의 사진을 올리면 피싱용 위조 문서의 특징이 있는지 알려준다.
 */
import { useState } from 'react';
import { Image, ScrollView, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import * as DocumentPicker from 'expo-document-picker';
import { AppText, keepAll } from '@/components/AppText';
import { Button } from '@/components/Button';
import { CheckResult, CheckSection } from '@/components/CheckResult';
import { Icon } from '@/components/Icon';
import { ScreenHeader } from '@/components/ScreenHeader';
import { useTheme } from '@/core/theme/theme';
import { friendlyNetworkError } from '@/core/utils/format';
import { checkDocument, type DocumentCheck, type PickedImage } from '@/data/services/analyzeApi';

export default function DocumentCheckScreen() {
  const { colors, isDark, fontScale } = useTheme();
  const [image, setImage] = useState<PickedImage | null>(null);
  const [note, setNote] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<DocumentCheck | null>(null);
  const brandFill = isDark ? colors.primaryDark : colors.primary;

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
      <ScrollView
        contentContainerStyle={{ paddingHorizontal: 20, paddingTop: 4, paddingBottom: 24, gap: 16 }}
        keyboardShouldPersistTaps="handled"
      >
        <AppText color={colors.textSecondary} style={{ fontSize: 15, lineHeight: 23 }}>
          검찰·경찰·금융감독원 공문, 영장, 대출 서류를 받았다면 사진을 올려 주세요.{'\n'}가짜 문서인지 확인해 드려요.
        </AppText>

        {image ? (
          <View style={{ gap: 12 }}>
            <Image
              source={{ uri: image.uri }}
              resizeMode="contain"
              accessibilityLabel="선택한 문서 이미지"
              style={{ width: '100%', height: 260, borderRadius: 16, backgroundColor: colors.cardAlt }}
            />
            <Button title="다른 사진 고르기" variant="outlined" icon="upload" onPress={pickImage} style={{ minHeight: 44 }} />
          </View>
        ) : (
          // 첫 단계(사진 고르기)가 바로 보이도록 음성 파일 분석과 같은 파란 올리기 카드로 보여준다.
          <View style={{ backgroundColor: brandFill, borderRadius: 20, padding: 30, alignItems: 'center' }}>
            <View
              style={{
                width: 62,
                height: 62,
                borderRadius: 18,
                backgroundColor: 'rgba(255,255,255,0.2)',
                alignItems: 'center',
                justifyContent: 'center',
                marginBottom: 16,
              }}
            >
              <Icon name="file" size={30} color="#FFFFFF" />
            </View>
            <AppText weight="800" color="#FFFFFF" style={{ fontSize: 18, lineHeight: 26 }}>
              문서 사진을 올려주세요
            </AppText>
            <AppText weight="600" color="rgba(255,255,255,0.92)" style={{ fontSize: 13, lineHeight: 18, marginTop: 8 }}>
              공문 · 영장 · 대출 서류 · 캡처
            </AppText>
            <Button
              title="문서 사진 고르기"
              variant="white"
              onPress={pickImage}
              style={{ marginTop: 16, minHeight: 44, paddingHorizontal: 28, alignSelf: 'center' }}
            />
          </View>
        )}

        <View style={{ gap: 8 }}>
          <AppText weight="700" style={{ fontSize: 15, lineHeight: 22 }}>
            어떻게 받았나요? (선택)
          </AppText>
          <TextInput
            value={note}
            onChangeText={setNote}
            placeholder="예: 검사라는 사람이 카톡으로 보냈어요"
            placeholderTextColor={colors.textMuted}
            multiline
            accessibilityLabel="상황 설명"
            style={[
              {
                minHeight: 64,
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

        {result && !result.readable ? (
          <View style={{ flexDirection: 'row', gap: 8, padding: 16, borderRadius: 16, backgroundColor: colors.warningFaint }}>
            <Icon name="alert-triangle" size={18} color={colors.warningText} />
            <AppText color={colors.warningText} style={{ fontSize: 14, lineHeight: 21, flex: 1 }}>
              문서 내용을 읽지 못했어요. 글자가 또렷하게 보이도록 다시 찍어서 올려 주세요.
            </AppText>
          </View>
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
              <CheckSection title="문서에서 확인한 내용">
                <View style={{ gap: 8 }}>
                  {result.evidence.map((item) => (
                    <AppText key={item} style={{ fontSize: 14, lineHeight: 21 }}>
                      · {item}
                    </AppText>
                  ))}
                </View>
              </CheckSection>
            ) : null}
            <AppText color={colors.textSecondary} style={{ fontSize: 13, lineHeight: 19, paddingHorizontal: 4 }}>
              {result.disclaimer}
            </AppText>
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
        {loading || !image ? (
          <AppText
            aria-live="polite"
            color={colors.textSecondary}
            style={{ fontSize: 14, lineHeight: 20, textAlign: 'center' }}
          >
            {loading ? '문서를 읽고 있어요. 10~20초쯤 걸려요.' : '먼저 문서 사진을 골라 주세요.'}
          </AppText>
        ) : null}
        <Button title="점검하기" onPress={run} loading={loading} disabled={!image} style={{ minHeight: 56 }} />
      </View>
    </SafeAreaView>
  );
}
