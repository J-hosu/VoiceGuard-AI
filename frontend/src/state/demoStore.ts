import { create } from 'zustand';

/**
 * 발표용 넓은 화면(웹)에서 앱 옆에 띄우는 "AI 분석 과정" 패널이 어느 통화를 보여줄지 정한다.
 * 실시간 화면이 통화 id와 진행 여부만 알려주고, 패널은 그 id로 서버 상태를 직접 조회한다.
 */
interface DemoState {
  logId: number; // 0이면 아직 통화 없음
  running: boolean;
  halted: boolean; // 연결 끊김·연결 실패·마이크 오류로 분석이 멈췄다(사용자가 끝낸 것이 아님)
  setCall: (logId: number, running: boolean, halted?: boolean) => void;
}

export const useDemoStore = create<DemoState>((set) => ({
  logId: 0,
  running: false,
  halted: false,
  setCall: (logId, running, halted = false) =>
    set((prev) =>
      prev.logId === logId && prev.running === running && prev.halted === halted ? prev : { logId, running, halted },
    ),
}));
