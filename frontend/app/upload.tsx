import { useEffect, useRef, useState } from 'react';
import { ActivityIndicator, Alert, Platform, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';
import * as DocumentPicker from 'expo-document-picker';
import { AppText } from '@/components/AppText';
import { Button } from '@/components/Button';
import { Card } from '@/components/Card';
import { Icon } from '@/components/Icon';
import { IconCircle } from '@/components/IconCircle';
import { ScreenHeader } from '@/components/ScreenHeader';
import { useTheme } from '@/core/theme/theme';
import { friendlyNetworkError } from '@/core/utils/format';
import { analyzeAudioFile } from '@/data/services/uploadAudio';
import {
  AUDIO_PICKER_TYPES_NATIVE,
  AUDIO_PICKER_TYPES_WEB,
  isAcceptedAudioName,
} from '@/data/services/uploadAudio.shared';
import { useCallStore } from '@/state/callStore';
import { useTranscriptStore } from '@/state/transcriptStore';
import type { TranscriptTurn } from '@/data/models/types';

function formatSize(bytes?: number | null): string {
  if (!bytes) return '';
  return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
}

export default function Upload() {
  const { colors } = useTheme();
  const router = useRouter();
  const fetchCalls = useCallStore((s) => s.fetchCalls);
  const saveTranscript = useTranscriptStore((s) => s.save);
  const [file, setFile] = useState<{ name: string; size?: number; uri?: string; mime?: string } | null>(
    null,
  );
  const [progress, setProgress] = useState(0);
  const [phase, setPhase] = useState('');
  const [analyzing, setAnalyzing] = useState(false);
  const [analyzingMsg, setAnalyzingMsg] = useState(false);
  const [errorMsg, setErrorMsg] = useState('');
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  const msgTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (timer.current) clearInterval(timer.current);
      if (msgTimer.current) clearTimeout(msgTimer.current);
    },
    [],
  );

  const pick = async () => {
    // 서버가 받는 형식(mp3·wav·m4a)만 고를 수 있게 한다. 웹은 확장자까지 넘겨야 브라우저가 같은 목록을 보인다.
    const res = await DocumentPicker.getDocumentAsync({
      type: Platform.OS === 'web' ? AUDIO_PICKER_TYPES_WEB : AUDIO_PICKER_TYPES_NATIVE,
      copyToCacheDirectory: true,
    });
    if (!res.canceled && res.assets?.[0]) {
      const a = res.assets[0];
      setProgress(0);
      setPhase('');
      // 고르기 창이 다른 파일을 허용해도(브라우저·기기마다 다르다) 서버는 이름의 확장자로 거절하므로 여기서 먼저 알린다.
      if (!isAcceptedAudioName(a.name)) {
        setFile(null);
        setErrorMsg('MP3, WAV, M4A 파일만 분석할 수 있어요. 다른 파일을 골라 주세요.');
        return;
      }
      setErrorMsg('');
      setFile({ name: a.name, size: a.size ?? undefined, uri: a.uri, mime: a.mimeType ?? undefined });
    }
  };

  const startProgressAnim = () => {
    timer.current = setInterval(() => {
      setProgress((p) => (p < 90 ? p + 3 : p));
    }, 220);
  };
  const stopProgressAnim = () => {
    if (timer.current) clearInterval(timer.current);
    timer.current = null;
  };

  const analyze = async () => {
    if (!file?.uri) {
      // 웹에서는 Alert가 뜨지 않으므로 화면 안내도 함께 바꾼다.
      setErrorMsg('분석할 음성 파일(MP3·WAV·M4A)을 먼저 골라 주세요.');
      Alert.alert('파일 선택 필요', '분석할 음성 파일(MP3·WAV·M4A)을 먼저 골라 주세요.');
      return;
    }
    setAnalyzing(true);
    setAnalyzingMsg(true);
    setErrorMsg('');
    setProgress(0);
    startProgressAnim();
    // "분석 중" 안내는 3초만 띄우고 사라지게 한다(실제 업로드/분석은 백그라운드로 계속).
    if (msgTimer.current) clearTimeout(msgTimer.current);
    msgTimer.current = setTimeout(() => setAnalyzingMsg(false), 3000);

    try {
      // 원본 오디오를 백엔드로 그대로 업로드 → 백엔드가 전사(STT) 후 위험도 채점.
      // 결과는 백엔드 DB에 저장되고 log_id가 발급된다(로컬 저장 없음).
      setPhase('분석 중');
      const analysis = await analyzeAudioFile({ uri: file.uri, name: file.name, mime: file.mime });
      if (analysis.segments.length === 0) throw new Error('전사된 발화가 없습니다. 파일을 확인하세요.');

      stopProgressAnim();
      setProgress(100);

      // 백엔드 전사가 발화별 화자(화자A=상대방, 화자B=나)를 내려주므로 role에 그대로 담는다.
      // 결과 화면(대화 내용 탭)이 role 기준으로 좌(상대방)/우(나) 말풍선을 그린다.
      const turns: TranscriptTurn[] = analysis.segments.map((s, i) => ({
        turnIndex: i,
        role: s.speaker,
        isMine: s.speaker === '화자B',
        content: s.text,
        atSec: s.start,
      }));
      saveTranscript(analysis.logId, turns);

      // 히스토리 목록을 백엔드에서 새로고침하고, 백엔드 log_id로 상세 화면 이동.
      void fetchCalls();
      setTimeout(() => router.replace(`/result?id=${analysis.logId}&score=${analysis.riskScore}`), 250);
    } catch (e) {
      stopProgressAnim();
      if (msgTimer.current) clearTimeout(msgTimer.current);
      setAnalyzing(false);
      setAnalyzingMsg(false);
      setProgress(0);
      setPhase('');
      // 서버·프록시 오류 원문(HTML 등)은 uploadAudio가 이미 사용자용 문장으로 바꿔 둔다. 남은 영문 네트워크 오류도 바꾼다.
      const msg = friendlyNetworkError(e instanceof Error ? e.message : String(e));
      setErrorMsg(`분석 실패: ${msg}`);
      Alert.alert('분석 실패', msg);
    }
  };

  return (
    <View style={{ flex: 1, backgroundColor: colors.bg }}>
      <SafeAreaView edges={['top']} style={{ flex: 1 }}>
        <ScreenHeader title="음성 파일 분석" />
        <View style={{ flex: 1, paddingHorizontal: 20, gap: 18 }}>
          <View style={{ backgroundColor: colors.primary, borderRadius: 20, padding: 30, alignItems: 'center' }}>
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
              <Icon name="upload" size={30} color="#FFFFFF" />
            </View>
            <AppText weight="800" color="#FFFFFF" style={{ fontSize: 18 }}>
              여기로 파일을 올려주세요
            </AppText>
            <AppText weight="600" color="rgba(255,255,255,0.85)" style={{ fontSize: 12.5, marginTop: 7 }}>
              MP3 · WAV · M4A · 2분 이하 권장
            </AppText>
            <Button
              title="파일 선택"
              variant="white"
              onPress={pick}
              style={{ marginTop: 18, minHeight: 44, paddingHorizontal: 28, alignSelf: 'center' }}
            />
          </View>

          {file ? (
            <View>
              <AppText weight="800" style={{ fontSize: 16, marginBottom: 11 }}>
                {analyzing ? phase || '처리 중' : '선택한 파일'}
              </AppText>
              <Card>
                <View style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
                  <IconCircle name="file" color={colors.primary} bg={colors.primaryFaint} size={40} radius={11} iconSize={20} />
                  <View style={{ flex: 1 }}>
                    <AppText weight="700" style={{ fontSize: 14.5 }} numberOfLines={1}>
                      {file.name}
                    </AppText>
                    <AppText color={colors.textMuted} style={{ fontSize: 12, marginTop: 2 }}>
                      {formatSize(file.size)}
                    </AppText>
                  </View>
                  <AppText weight="800" color={colors.primary} style={{ fontSize: 16 }}>
                    {progress}%
                  </AppText>
                </View>
                <View style={{ height: 7, borderRadius: 4, backgroundColor: colors.cardAlt, marginTop: 13, overflow: 'hidden' }}>
                  <View style={{ width: `${progress}%`, height: '100%', borderRadius: 4, backgroundColor: colors.primary }} />
                </View>
              </Card>
            </View>
          ) : null}

          {analyzingMsg ? (
            <View
              style={{
                flexDirection: 'row',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 10,
                paddingVertical: 14,
              }}
            >
              <ActivityIndicator color={colors.primary} />
              <AppText weight="700" color={colors.primary} style={{ fontSize: 15 }}>
                분석 중…
              </AppText>
            </View>
          ) : null}

          {errorMsg ? (
            <View style={{ backgroundColor: colors.dangerFaint, borderRadius: 12, padding: 14 }}>
              <AppText weight="700" color={colors.danger} style={{ fontSize: 13.5, lineHeight: 20 }}>
                {errorMsg}
              </AppText>
            </View>
          ) : null}
        </View>

        <View style={{ padding: 20 }}>
          <Button
            title={analyzing ? phase || '분석 중...' : '분석 시작'}
            onPress={analyze}
            loading={analyzing}
            style={{ minHeight: 56 }}
          />
        </View>
      </SafeAreaView>
    </View>
  );
}
