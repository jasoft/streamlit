import sqlite3 from 'sqlite3';
import { open, Database } from 'sqlite';
import path from 'path';

let db: Database | null = null;

export async function getDb() {
  if (db) return db;
  db = await open({
    filename: path.join(process.cwd(), 'fund_flow_cache.db'),
    driver: sqlite3.Database
  });
  
  await db.exec(`
    CREATE TABLE IF NOT EXISTS snapshot_cache (
      id TEXT PRIMARY KEY,
      data TEXT,
      updated_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS kline_cache (
      id TEXT PRIMARY KEY,
      data TEXT,
      updated_at INTEGER
    );
  `);
  
  return db;
}
