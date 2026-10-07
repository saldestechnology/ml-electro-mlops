import '@testing-library/jest-dom/vitest';
import { resetSelection } from '../store/selection';

// the selection store is module state: start every test from the default
beforeEach(() => {
  resetSelection();
});
