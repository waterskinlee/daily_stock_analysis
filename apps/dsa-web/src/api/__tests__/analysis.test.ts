import { beforeEach, describe, expect, it, vi } from 'vitest';
import { analysisApi, scheduledRunApi } from '../analysis';

const post = vi.hoisted(() => vi.fn());

vi.mock('../index', () => ({
  default: {
    get: vi.fn(),
    post,
  },
}));

describe('analysisApi.triggerMarketReview', () => {
  beforeEach(() => {
    post.mockReset();
    post.mockResolvedValue({
      status: 202,
      data: {
        status: 'accepted',
        message: 'accepted',
        send_notification: true,
        region: 'cn,us',
        task_id: 'market-task-1',
      },
    });
  });

  it('serializes selected markets to a comma-separated request string', async () => {
    const result = await analysisApi.triggerMarketReview({
      sendNotification: false,
      regions: ['cn', 'us'],
    });

    expect(post).toHaveBeenCalledWith(
      '/api/v1/analysis/market-review',
      {
        send_notification: false,
        report_language: undefined,
        region: 'cn,us',
      },
      expect.any(Object),
    );
    expect(result.region).toBe('cn,us');
  });

  it('omits region when the caller inherits the server default', async () => {
    await analysisApi.triggerMarketReview({ sendNotification: true });

    expect(post).toHaveBeenCalledWith(
      '/api/v1/analysis/market-review',
      {
        send_notification: true,
        report_language: undefined,
      },
      expect.any(Object),
    );
  });
});

describe('analysisApi.cancelTask', () => {
  beforeEach(() => {
    post.mockReset();
  });

  it('posts an encoded task id and converts the returned task payload', async () => {
    post.mockResolvedValue({
      status: 200,
      data: {
        task_id: 'task/1',
        stock_code: '600519',
        status: 'cancel_requested',
        progress: 42,
        message: '正在取消...',
        report_type: 'detailed',
        created_at: '2026-08-17T08:00:00Z',
      },
    });

    const result = await analysisApi.cancelTask('task/1');

    expect(post).toHaveBeenCalledWith('/api/v1/analysis/tasks/task%2F1/cancel');
    expect(result.taskId).toBe('task/1');
    expect(result.status).toBe('cancel_requested');
  });
});

describe('scheduledRunApi.cancelRun', () => {
  beforeEach(() => {
    post.mockReset();
  });

  it('requests cancellation and returns the normalized run status', async () => {
    post.mockResolvedValue({
      status: 200,
      data: {
        run: {
          run_id: 'run/1',
          status: 'cancel_requested',
          stock_count: 8,
          completed_count: 2,
          started_at: null,
          finished_at: null,
          error: null,
        },
      },
    });

    const result = await scheduledRunApi.cancelRun('run/1');

    expect(post).toHaveBeenCalledWith('/api/v1/analysis/scheduled-runs/run%2F1/cancel');
    expect(result.runId).toBe('run/1');
    expect(result.status).toBe('cancel_requested');
  });
});
