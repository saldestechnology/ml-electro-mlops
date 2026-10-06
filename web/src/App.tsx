import { useMemo } from 'react';
import { Route, Routes } from 'react-router';
import type { ApiClient } from './api';
import { Layout } from './components/Layout';
import { ApiContext, useApi } from './hooks/useApi';
import { StatusContext } from './hooks/useStatus';
import { ForecastPage } from './pages/ForecastPage';
import { ModelPage } from './pages/ModelPage';
import { NotFound } from './pages/NotFound';
import { PerformancePage } from './pages/PerformancePage';

function StatusProvider({ children }: { children: React.ReactNode }) {
  const health = useApi('health', (c, s) => c.health(s));
  const zones = useApi('zones', (c, s) => c.zones(s));
  const value = useMemo(() => ({ health, zones }), [health, zones]);
  return <StatusContext.Provider value={value}>{children}</StatusContext.Provider>;
}

/** The app without a router, so tests can wrap it in a MemoryRouter. */
export function App({ client }: { client: ApiClient }) {
  return (
    <ApiContext.Provider value={client}>
      <StatusProvider>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<ForecastPage />} />
            <Route path="performance" element={<PerformancePage />} />
            <Route path="model" element={<ModelPage />} />
            <Route path="*" element={<NotFound />} />
          </Route>
        </Routes>
      </StatusProvider>
    </ApiContext.Provider>
  );
}
