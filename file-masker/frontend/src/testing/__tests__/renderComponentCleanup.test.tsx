import { describe, expect, it } from 'vitest';
import { useEffect, useRef, useState } from 'react';
import { renderComponent } from '../renderComponent';

/**
 * 渲染完不 unmount 的组件会把定时器留着；测试文件跑完、环境被拆掉之后定时器才触发，
 * 就会撞上一个已经不存在的 `window` —— vitest 记成 unhandled error 并让整次运行失败，
 * 哪怕每个用例都是绿的。`compliance-tools-frontend-test` #249 正是这么挂的。
 *
 * 这两个用例钉住「helper 会替你收尾」：第一个只渲染、故意不 unmount，第二个检查它已经没了。
 */
let timerFiredAfterUnmount = false;

function LeaksATimer() {
  const [, setTick] = useState(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    timer.current = setTimeout(() => {
      // 组件若已卸载, 这里不该再跑 —— 跑了就说明 cleanup 没生效。
      timerFiredAfterUnmount = true;
      setTick((t) => t + 1);
    }, 50);
    return () => { if (timer.current) clearTimeout(timer.current); };
  }, []);
  return <div className="leaky" />;
}

describe('renderComponent 的自动收尾', () => {
  it('renders_andDeliberatelyDoesNotUnmount', async () => {
    const r = await renderComponent(<LeaksATimer />);
    expect(r.find('.leaky')).not.toBeNull();
    // 故意不调 r.unmount()
  });

  it('previousTestsTreeIsGone_andItsTimerNeverFired', async () => {
    expect(document.querySelectorAll('.leaky')).toHaveLength(0);
    // 等过上一节里那个 50ms 定时器的窗口 —— cleanup 清掉了它, 所以不该触发。
    await new Promise((resolve) => { setTimeout(resolve, 120); });
    expect(timerFiredAfterUnmount).toBe(false);
  });
});
