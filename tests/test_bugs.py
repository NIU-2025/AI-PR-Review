"""
用户管理模块 - 包含多个故意注入的代码缺陷

此文件用于测试 AI PR Review 系统的 Bug 检测能力。
预期应被检测到的风险:
  - P0 [安全] SQL注入
  - P0 [安全] 硬编码密钥
  - P1 [稳定性] 资源未释放
  - P1 [逻辑] 潜在的无限循环
  - P1 [稳定性] 异常吞没
  - P2 [性能] 循环内数据库查询 (N+1)
"""

#再次提交做CodeRabbit测试用

import sqlite3
import hashlib
import time


# ──────────────────────────────────────────────
# Bug 1: SQL 注入 (P0 [安全])
# ──────────────────────────────────────────────

def get_users_by_name(db_path, search_name):
    """
    根据用户名查询用户列表

    Bug: 使用字符串拼接构造 SQL, 攻击者可通过 search_name 注入恶意 SQL。
    例如: search_name = "admin' OR '1'='1" 可绕过认证获取所有用户数据。
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # ⚠️ 风险代码: SQL 拼接, 应使用参数化查询
    query = f"SELECT id, name, email, role FROM users WHERE name LIKE '%{search_name}%'"

    try:
        cursor.execute(query)
        results = cursor.fetchall()
        return results
    finally:
        conn.close()


def update_user_profile(db_path, user_id, nickname):
    """
    更新用户昵称

    Bug: 同样的 SQL 拼接问题, UPDATE 语句同样存在注入风险。
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # ⚠️ 风险代码: UPDATE 拼接
    query = f"UPDATE users SET nickname = '{nickname}' WHERE id = {user_id}"

    cursor.execute(query)
    conn.commit()
    conn.close()
    return True


# ──────────────────────────────────────────────
# Bug 2: 硬编码密钥 (P0 [安全])
# ──────────────────────────────────────────────

# ⚠️ 风险代码: 硬编码的 API 密钥和数据库凭证, 一旦泄露攻击者可直接访问系统
SECRET_API_KEY = "sk-live-8a7b9c3d4e5f6g7h8i9j0k1l2m3n4o5p"
DATABASE_PASSWORD = "admin123!@#"
ENCRYPTION_SALT = "mysalt_2024"

def call_external_api(user_data):
    """
    调用外部 API 发送用户数据

    Bug: 密钥硬编码在代码中, 反编译或代码泄露即暴露。
    应使用环境变量或密钥管理服务。
    """
    headers = {
        "Authorization": f"Bearer {SECRET_API_KEY}",
        "Content-Type": "application/json",
    }
    # 实际应发 HTTP 请求, 这里省略具体实现
    return headers


def hash_password(password):
    """
    对密码进行哈希处理

    Bug: 使用固定 salt 的 MD5, MD5 已被证明不安全,
    应使用 bcrypt 或 argon2。
    """
    salted = password + ENCRYPTION_SALT
    return hashlib.md5(salted.encode()).hexdigest()


# ──────────────────────────────────────────────
# Bug 3: 潜在的无限循环 (P1 [逻辑])
# ──────────────────────────────────────────────

def poll_task_status(status_checker, task_id):
    """
    轮询异步任务状态直到完成

    Bug: 如果 status_checker 始终不返回 'done' 或 'failed',
    循环永远不退出, 线程会永久阻塞。
    缺少最大重试次数和超时保护。
    """
    attempts = 0
    while True:
        status = status_checker(task_id)
        attempts += 1

        if status == "done":
            return "success"
        elif status == "failed":
            return "error"

        # ⚠️ 风险代码: 没有 break 条件!
        # 如果 status 既不是 'done' 也不是 'failed',
        # 这个循环永远不会退出。
        print(f"第 {attempts} 次检查, 状态: {status}")
        time.sleep(1)


# ──────────────────────────────────────────────
# Bug 4: 资源未释放 (P1 [稳定性])
# ──────────────────────────────────────────────

def export_user_report(db_path, output_path):
    """
    导出用户报告到文件

    Bug: 打开文件后仅在 try 中 close, 如果 write 失败,
    文件句柄不会释放, 多次执行后可能耗尽文件描述符。
    应使用 with open() 语句。
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users")

    # ⚠️ 风险代码: 如果 write 抛出异常, f.close() 不会被执行
    f = open(output_path, "w", encoding="utf-8")
    for row in cursor:
        f.write(str(row) + "\n")

    f.close()
    conn.close()


# ──────────────────────────────────────────────
# Bug 5: 异常吞没 (P1 [稳定性])
# ──────────────────────────────────────────────

def delete_user(db_path, user_id):
    """
    删除用户

    Bug: 捕获所有异常后仅打印, 调用方完全不知道删除是否成功。
    如果数据库连接失败、SQL 语法错误, 错误都被默默吞没。
    """
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(f"DELETE FROM users WHERE id = {user_id}")
        conn.commit()
        conn.close()
        return True
    except:
        # ⚠️ 风险代码: 裸 except + 只 print 不处理
        print("删除用户时出现错误")
        return False


# ──────────────────────────────────────────────
# Bug 6: N+1 查询 (P2 [性能])
# ──────────────────────────────────────────────

def get_users_with_orders(db_path):
    """
    获取所有用户及其订单

    Bug: 先查询所有用户, 然后对每个用户再查一次订单。
    1000 个用户会产生 1001 次数据库查询。
    应使用 JOIN 一次性查询。
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("SELECT id, name FROM users")
    users = cursor.fetchall()

    result = []
    for user in users:
        user_id = user[0]
        # ⚠️ 风险代码: 循环内执行数据库查询 (N+1 问题)
        cursor.execute(f"SELECT * FROM orders WHERE user_id = {user_id}")
        orders = cursor.fetchall()
        result.append({"user": user[1], "orders": len(orders)})

    conn.close()
    return result
