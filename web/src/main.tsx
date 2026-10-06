import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router';
// Global styles first, so component stylesheets (imported by App) come later in the cascade.
import './styles/fonts.css';
import './styles/tokens.css';
import './styles/base.css';
import { defaultClient } from './api';
import { App } from './App';

const root = document.getElementById('root');
if (!root) throw new Error('#root missing');

void defaultClient().then((client) => {
  createRoot(root).render(
    <StrictMode>
      <BrowserRouter>
        <App client={client} />
      </BrowserRouter>
    </StrictMode>,
  );
});
