import { DatabaseSync } from "node:sqlite";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
export function openStore(file: string) {
  mkdirSync(dirname(file), { recursive: true, mode: 0o700 });
  const db = new DatabaseSync(file);
  db.exec(`PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;
 CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,workspace TEXT NOT NULL,csrf TEXT NOT NULL,wallet TEXT,expires INTEGER NOT NULL);
 CREATE TABLE IF NOT EXISTS wallets(address TEXT PRIMARY KEY,workspace TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS agents(id TEXT PRIMARY KEY,workspace TEXT NOT NULL,name TEXT NOT NULL,role TEXT NOT NULL,instructions TEXT NOT NULL,created TEXT NOT NULL,updated TEXT NOT NULL,archived INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,workspace TEXT NOT NULL,agent TEXT NOT NULL,network TEXT NOT NULL,author TEXT NOT NULL,text TEXT NOT NULL,created TEXT NOT NULL);
 CREATE INDEX IF NOT EXISTS message_scope ON messages(workspace,agent,network,created);
 CREATE TABLE IF NOT EXISTS message_cards(message_id TEXT PRIMARY KEY REFERENCES messages(id),body TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,workspace TEXT NOT NULL,agent TEXT NOT NULL,network TEXT NOT NULL,message_id TEXT NOT NULL,status TEXT NOT NULL,error TEXT,created TEXT NOT NULL,updated TEXT NOT NULL,model TEXT,input_tokens INTEGER,output_tokens INTEGER,latency_ms INTEGER);
 CREATE TABLE IF NOT EXISTS challenges(id TEXT PRIMARY KEY,session TEXT NOT NULL,address TEXT NOT NULL,network TEXT NOT NULL,message TEXT NOT NULL,expires INTEGER NOT NULL,used INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS idempotency(scope TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,response TEXT NOT NULL,created INTEGER NOT NULL);
 `);
  db.prepare(
    "UPDATE jobs SET status='FAILED',error='Service restarted during generation. Send another message to continue.' WHERE status='RUNNING'",
  ).run();
  return db;
}
