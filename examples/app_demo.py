"""演示用的「问题代码」——埋入多个典型缺陷，用于在线审查链路与演示视频。

注意：本文件刻意保留所有问题，仅作 Agent 审查素材，请勿在生产使用。
"""

import os
import sqlite3

# 缺陷1：硬编码密钥
API_KEY = "sk-hardcoded-secret-12345"


def find_user(users, name):
    """缺陷2：循环内重复做全表遍历且空指针风险。"""
    for i in range(len(users)):
        if users[i]["name"] == name:
            return users[i]
    return None


def run_expr(expr):
    """缺陷3：eval 注入。"""
    return eval(expr)


def query_user(db, user_id):
    """缺陷4：SQL 字符串拼接注入。"""
    sql = "SELECT * FROM users WHERE id = " + user_id
    return db.execute(sql)


def read_file(path):
    """缺陷5：裸 except 吞掉所有异常 + 文件未关闭。"""
    try:
        f = open(path)
        return f.read()
    except:
        pass


def remove_tmp():
    """缺陷6：os.system 命令拼接（若参数来自用户则为命令注入）。"""
    os.system("rm -rf /tmp/cache")
