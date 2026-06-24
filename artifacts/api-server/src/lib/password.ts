import { randomBytes, scryptSync, timingSafeEqual } from "node:crypto";

const KEY_LENGTH = 64;
const PREFIX = "scrypt";

/**
 * Hash a plaintext password using scrypt (no native dependency).
 * Format: `scrypt$<saltHex>$<hashHex>`.
 */
export function hashPassword(plain: string): string {
  const salt = randomBytes(16).toString("hex");
  const hash = scryptSync(plain, salt, KEY_LENGTH).toString("hex");
  return `${PREFIX}$${salt}$${hash}`;
}

/**
 * Verify a plaintext password against a stored `scrypt$salt$hash` string.
 * Uses a constant-time comparison.
 */
export function verifyPassword(plain: string, stored: string): boolean {
  const parts = stored.split("$");
  if (parts.length !== 3 || parts[0] !== PREFIX) return false;

  const [, salt, hashHex] = parts;
  const expected = Buffer.from(hashHex, "hex");
  const actual = scryptSync(plain, salt, expected.length);

  return expected.length === actual.length && timingSafeEqual(expected, actual);
}
