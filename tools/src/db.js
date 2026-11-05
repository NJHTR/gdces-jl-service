// 数据库连接（只读查询用池，写操作也走它）
import mysql from "mysql2/promise";

const cfg = {
  host: process.env.MYSQL_HOST || "jl-mysql",
  port: Number(process.env.MYSQL_PORT || 3306),
  user: process.env.MYSQL_USER || "jl",
  password: process.env.MYSQL_PASSWORD || "",
  database: process.env.MYSQL_DATABASE || "jl",
  charset: "utf8mb4",
  timezone: "+08:00",
  connectionLimit: 5,
  enableKeepAlive: true,
};

let pool = null;

export function db() {
  if (!pool) pool = mysql.createPool(cfg);
  return pool;
}

export async function q(sql, args = []) {
  const [rows] = await db().query(sql, args);
  return rows;
}

export async function one(sql, args = []) {
  const rows = await q(sql, args);
  return rows[0] || null;
}

export async function health() {
  try {
    await q("SELECT 1");
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}
