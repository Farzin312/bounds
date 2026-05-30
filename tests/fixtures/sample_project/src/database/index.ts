// Database subsystem — owns user persistence. Exposes findUser + createUser.

export function findUser(id: string): string | null {
  return repo.get(id);
}

export function createUser(name: string): string {
  return repo.add(name);
}

// Internal: not part of the declared public surface.
export class UserRepository {
  private store = new Map<string, string>();
  get(id: string): string | null {
    return this.store.get(id) ?? null;
  }
  add(name: string): string {
    const id = String(this.store.size + 1);
    this.store.set(id, name);
    return id;
  }
}

const repo = new UserRepository();
