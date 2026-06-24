import { index, json, pgTable, timestamp, varchar } from "drizzle-orm/pg-core";

// Session store table for express-session via connect-pg-simple.
// connect-pg-simple owns the row data at runtime, but we own the DDL here so
// that `drizzle-kit push` creates it (and never tries to drop it). The shape
// must match connect-pg-simple's expected schema: sid (pk), sess (json),
// expire (timestamp) with an index on expire for pruning.
export const sessionTable = pgTable(
  "session",
  {
    sid: varchar("sid").primaryKey(),
    sess: json("sess").notNull(),
    expire: timestamp("expire", { precision: 6 }).notNull(),
  },
  (table) => [index("IDX_session_expire").on(table.expire)],
);
