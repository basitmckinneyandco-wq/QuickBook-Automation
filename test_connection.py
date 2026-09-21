import win32com.client

qb = win32com.client.Dispatch("QBXMLRP2.RequestProcessor")
qb.OpenConnection2("", "My First QB Python App", 1)
ticket = qb.BeginSession("", 2)

print("✅ Connected successfully! Ticket:", ticket)

qb.EndSession(ticket)
qb.CloseConnection()
print("✅ Disconnected cleanly.")