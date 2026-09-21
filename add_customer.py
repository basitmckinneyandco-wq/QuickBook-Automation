import win32com.client

def get_qb_connection():
    qb = win32com.client.Dispatch("QBXMLRP2.RequestProcessor")
    qb.OpenConnection2("", "My First QB Python App", 1)
    ticket = qb.BeginSession("", 2)
    return qb, ticket

def close_qb_connection(qb, ticket):
    qb.EndSession(ticket)
    qb.CloseConnection()

def build_customer_add_xml(name, company, email):
    return f"""<?xml version="1.0" encoding="utf-8"?>
<?qbxml version="16.0"?>
<QBXML>
  <QBXMLMsgsRq onError="stopOnError">
    <CustomerAddRq>
      <CustomerAdd>
        <Name>{name}</Name>
        <CompanyName>{company}</CompanyName>
        <Email>{email}</Email>
      </CustomerAdd>
    </CustomerAddRq>
  </QBXMLMsgsRq>
</QBXML>"""

qb, ticket = get_qb_connection()
try:
    request_xml = build_customer_add_xml("Ibad Damji", "Doe Consulting LLC", "ibad@example.com")
    response_xml = qb.ProcessRequest(ticket, request_xml)
    print(response_xml)
finally:
    close_qb_connection(qb, ticket)