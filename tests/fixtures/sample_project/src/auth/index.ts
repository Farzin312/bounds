// Auth subsystem — login/register/verify. Consumes the database boundary.

import { findUser, createUser } from '../database';

export function login(user: string): boolean {
  return findUser(user) !== null;
}

export function register(name: string): string {
  return createUser(name);
}

export function verify(token: string): boolean {
  return token.length > 0;
}
