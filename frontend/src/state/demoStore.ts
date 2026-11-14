import { create } from 'zustand';

/**
 * 발표용 넓은 화면(웹)에서 앱 옆에 띄우는 "AI 분석 과정" 패널이 어느 통화를 보여줄지 정한다.
 * 실시간 화면이 통화 id와 진행 여부만 알려주고, 패널은 그 id로 서버 상태를 직접 조회한다.
 */
interface DemoState {
  logId: number; // 0이면 아직 통화 없음
  running: boolean;
  setCall: (logId: number, running: boolean) => void;
}

export const useDemoStore = create<DemoState>((set) => ({
  logId: 0,
  running: false,
  setCall: (logId, running) => set((prev) => (prev.logId === logId && prev.running === running ? prev : { logId, running })),
}));
