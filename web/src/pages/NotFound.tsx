import { Link } from 'react-router';
import { EmptyState } from '../components/States';

export function NotFound() {
  return (
    <div className="grid">
      <EmptyState title="No such page">
        <p>
          <Link to="/">Back to the forecast</Link>
        </p>
      </EmptyState>
    </div>
  );
}
