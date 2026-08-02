const clientIdStorageKey = "manyselves.clientId.v1";

export function getOrCreateBrowserClientId(
  storage: Pick<Storage, "getItem" | "setItem">,
  randomUuid: () => string = () => crypto.randomUUID(),
): string {
  const existing = storage.getItem(clientIdStorageKey);
  if (existing) {
    return existing;
  }
  const clientId = randomUuid();
  storage.setItem(clientIdStorageKey, clientId);
  return clientId;
}
