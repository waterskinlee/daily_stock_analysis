import { render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { scheduledRunApi } from '../../../api/analysis';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import { UI_LANGUAGE_STORAGE_KEY } from '../../../utils/uiLanguage';
import { ScheduledRunBanner } from '../ScheduledRunBanner';

vi.mock('../../../api/analysis', () => ({
  scheduledRunApi: {
    listActiveRuns: vi.fn(),
    getRunFlow: vi.fn(),
  },
}));

const listActiveRunsMock = vi.mocked(scheduledRunApi.listActiveRuns);

function renderBanner() {
  return render(
    <UiLanguageProvider>
      <ScheduledRunBanner />
    </UiLanguageProvider>
  );
}

describe('ScheduledRunBanner', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.setItem(UI_LANGUAGE_STORAGE_KEY, 'zh');
  });

  it('renders nothing when no scheduled run is active', async () => {
    listActiveRunsMock.mockResolvedValue([]);
    const { container } = renderBanner();

    await waitFor(() => expect(container.firstChild).toBeNull());
  });

  it('shows active run count and run id', async () => {
    listActiveRunsMock.mockResolvedValue([
      {
        runId: 'run-123',
        status: 'running',
        stockCount: 12,
        completedCount: 0,
        startedAt: null,
        finishedAt: null,
        error: null,
      },
    ]);
    renderBanner();

    expect(await screen.findByText('定时分析进行中')).toBeInTheDocument();
    expect(screen.getByText('共 12 只股票')).toBeInTheDocument();
    expect(screen.getByText(/run-123/)).toBeInTheDocument();
  });

  it('shows failure message when status fetch fails', async () => {
    listActiveRunsMock.mockRejectedValue(new Error('network'));
    renderBanner();

    expect(await screen.findByText('定时任务状态加载失败')).toBeInTheDocument();
  });
});
