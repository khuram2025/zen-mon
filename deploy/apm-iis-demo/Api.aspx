<%@ Page Language="C#" %>
<%@ Import Namespace="System" %>
<%@ Import Namespace="System.Net" %>
<%@ Import Namespace="System.Threading" %>
<script runat="server">
protected void Page_Load(object sender, EventArgs e)
{
    Response.ContentType = "application/json";
    Response.Cache.SetCacheability(HttpCacheability.NoCache);
    string action = (Request.QueryString["action"] ?? "success").ToLowerInvariant();
    if (action == "slow")
    {
        Thread.Sleep(850);
        Response.Write("{\"ok\":true,\"operation\":\"slow\",\"delay_ms\":850}");
        return;
    }
    if (action == "dependency")
    {
        using (var client = new WebClient())
        {
            client.UseDefaultCredentials = true;
            client.Headers[HttpRequestHeader.UserAgent] = "ZenPlus-IIS-APM-Demo/1.0";
            string body = client.DownloadString("http://127.0.0.1/LocalAuthTest/");
            Response.Write("{\"ok\":true,\"operation\":\"dependency\",\"bytes\":" + body.Length + "}");
        }
        return;
    }
    if (action == "error")
    {
        throw new InvalidOperationException("ZenPlus controlled demo exception");
    }
    Thread.Sleep(25);
    Response.Write("{\"ok\":true,\"operation\":\"success\",\"server\":\"" + Environment.MachineName + "\"}");
}
</script>
