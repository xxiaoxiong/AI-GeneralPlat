#!/usr/bin/env python3
"""手动完成迁移：添加 agents.database_connection_id 列"""
import pymysql

conn = pymysql.connect(
    host='localhost', 
    port=3308, 
    user='root', 
    password='password', 
    database='ai_plat'
)
cursor = conn.cursor()

# 1. 检查 agents 表是否已有 database_connection_id 列
cursor.execute('DESCRIBE agents')
columns = [row[0] for row in cursor.fetchall()]
print(f'Existing columns: {columns}')

if 'database_connection_id' not in columns:
    print('Adding database_connection_id column...')
    cursor.execute('ALTER TABLE agents ADD COLUMN database_connection_id INTEGER NULL')
    try:
        cursor.execute('ALTER TABLE agents ADD CONSTRAINT fk_agents_database_connection_id FOREIGN KEY (database_connection_id) REFERENCES database_connections(id)')
    except Exception as e:
        print(f'Foreign key may already exist or error: {e}')
    print('Column added successfully')
else:
    print('Column already exists')

conn.commit()
cursor.close()
conn.close()
print('Done!')
