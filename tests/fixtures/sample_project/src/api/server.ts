// API subsystem — the HTTP entrypoint. Consumes the auth boundary.

import { login, verify } from '../auth';

export function startServer(port: number): void {
  const ready = login('demo') || verify('token');
  console.log(`listening on ${port}; auth ready: ${ready}`);
}
