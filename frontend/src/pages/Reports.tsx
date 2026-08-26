import { useState, useEffect } from "react";
import { Download, FileText, Activity, ShieldAlert, Server } from "lucide-react";
import { api } from "../lib/api";
import { useAuth, isAdmin } from "../lib/auth";

export default function Reports() {
  const { user } = useAuth();
  
  // Uptime Report state
  const [uptimeDays, setUptimeDays] = useState(30);
  const [uptimeFormat, setUptimeFormat] = useState("pdf");
  const [uptimeDownloading, setUptimeDownloading] = useState(false);
  const [uptimeTenantId, setUptimeTenantId] = useState(""); // "" means own tenant or all for MSP
  
  // Compliance Report state
  const [complianceDays, setComplianceDays] = useState(30);
  const [complianceFormat, setComplianceFormat] = useState("pdf");
  const [complianceDownloading, setComplianceDownloading] = useState(false);

  // Tenants data for MSP staff
  const [tenants, setTenants] = useState<{id: string; name: string}[]>([]);

  useEffect(() => {
    if (user?.is_msp_staff) {
      api.get("/tenants").then(res => setTenants(res.data)).catch(() => {});
    }
  }, [user]);

  const downloadFile = (url: string, filename: string) => {
    return api.get(url, { responseType: 'blob' })
      .then((response) => {
        const url = window.URL.createObjectURL(new Blob([response.data]));
        const link = document.createElement('a');
        link.href = url;
        link.setAttribute('download', filename);
        document.body.appendChild(link);
        link.click();
        link.parentNode?.removeChild(link);
      })
      .catch((err) => {
        alert("Download failed. The service might be temporarily unavailable.");
        console.error(err);
      });
  };

  const handleDownloadUptime = () => {
    setUptimeDownloading(true);
    const params = new URLSearchParams();
    params.set("days", uptimeDays.toString());
    params.set("format", uptimeFormat);
    if (uptimeTenantId) {
      params.set("tenant_id", uptimeTenantId);
    }
    
    const timestamp = new Date().toISOString().split('T')[0].replace(/-/g, '');
    const filename = `netguard-uptime-report-${uptimeDays}d-${timestamp}.${uptimeFormat}`;
    
    downloadFile(`/reports/uptime-incident?${params.toString()}`, filename).finally(() => setUptimeDownloading(false));
  };

  const handleDownloadCompliance = () => {
    setComplianceDownloading(true);
    const params = new URLSearchParams();
    params.set("days", complianceDays.toString());
    params.set("format", complianceFormat);
    
    const timestamp = new Date().toISOString().split('T')[0].replace(/-/g, '');
    const filename = `netguard-compliance-report-${timestamp}.${complianceFormat}`;
    
    downloadFile(`/reports/compliance?${params.toString()}`, filename).finally(() => setComplianceDownloading(false));
  };

  return (
    <div className="p-6 max-w-7xl mx-auto">
      <div className="mb-8">
        <h1 className="text-2xl font-semibold text-slate-900 dark:text-white">Reports</h1>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          Generate and download historical metric, incident, and compliance reports.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* Uptime & Incident Report Card */}
        <div className="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-lg shadow-sm overflow-hidden flex flex-col">
          <div className="p-6 flex items-start gap-4 flex-1">
            <div className="p-3 bg-blue-50 dark:bg-blue-900/20 text-blue-600 dark:text-blue-400 rounded-lg shrink-0">
              <Activity className="w-6 h-6" />
            </div>
            <div className="flex-1">
              <h2 className="text-lg font-medium text-slate-900 dark:text-white">Uptime & Incident Report</h2>
              <p className="mt-1 text-sm text-slate-500 dark:text-slate-400 mb-6">
                Device availability percentages, outage counts, incident MTTA/MTTR, and a full incident log.
              </p>
              
              <div className="space-y-4">
                {user?.is_msp_staff && (
                  <div>
                    <label className="block text-xs font-medium text-slate-700 dark:text-slate-300 mb-1.5 uppercase tracking-wider">
                      Scope
                    </label>
                    <select
                      value={uptimeTenantId}
                      onChange={(e) => setUptimeTenantId(e.target.value)}
                      className="w-full form-select focus:ring-indigo-500 focus:border-indigo-500 block sm:text-sm border-slate-300 dark:border-slate-700 rounded-md bg-white dark:bg-slate-950 dark:text-white"
                    >
                      <option value="">Cross-Tenant Overview</option>
                      {tenants.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
                    </select>
                  </div>
                )}
                
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-xs font-medium text-slate-700 dark:text-slate-300 mb-1.5 uppercase tracking-wider">
                      Window
                    </label>
                    <select
                      value={uptimeDays}
                      onChange={(e) => setUptimeDays(Number(e.target.value))}
                      className="w-full form-select focus:ring-indigo-500 focus:border-indigo-500 block sm:text-sm border-slate-300 dark:border-slate-700 rounded-md bg-white dark:bg-slate-950 dark:text-white"
                    >
                      <option value={7}>Last 7 Days</option>
                      <option value={30}>Last 30 Days</option>
                      <option value={90}>Last 90 Days</option>
                    </select>
                  </div>
                  
                  <div>
                    <label className="block text-xs font-medium text-slate-700 dark:text-slate-300 mb-1.5 uppercase tracking-wider">
                      Format
                    </label>
                    <select
                      value={uptimeFormat}
                      onChange={(e) => setUptimeFormat(e.target.value)}
                      className="w-full form-select focus:ring-indigo-500 focus:border-indigo-500 block sm:text-sm border-slate-300 dark:border-slate-700 rounded-md bg-white dark:bg-slate-950 dark:text-white"
                    >
                      <option value="pdf">PDF Document</option>
                      <option value="csv">CSV Spreadsheet</option>
                    </select>
                  </div>
                </div>
              </div>
            </div>
          </div>
          <div className="px-6 py-4 bg-slate-50 dark:bg-slate-800/50 border-t border-slate-200 dark:border-slate-800 flex justify-end">
            <button
              onClick={handleDownloadUptime}
              disabled={uptimeDownloading}
              className="inline-flex items-center gap-2 px-4 py-2 bg-white dark:bg-slate-800 border border-slate-300 dark:border-slate-700 hover:bg-slate-50 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-200 text-sm font-medium rounded-md shadow-sm transition-colors disabled:opacity-50"
            >
              {uptimeDownloading ? (
                <div className="animate-spin w-4 h-4 border-2 border-slate-300 border-t-indigo-600 rounded-full" />
              ) : (
                <Download className="w-4 h-4" />
              )}
              Download Uptime Report
            </button>
          </div>
        </div>

        {/* Compliance Report Card */}
        <div className="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-lg shadow-sm overflow-hidden flex flex-col">
          <div className="p-6 flex items-start gap-4 flex-1">
            <div className="p-3 bg-indigo-50 dark:bg-indigo-900/20 text-indigo-600 dark:text-indigo-400 rounded-lg shrink-0">
              <ShieldAlert className="w-6 h-6" />
            </div>
            <div className="flex-1">
              <h2 className="text-lg font-medium text-slate-900 dark:text-white">Fleet Compliance Report</h2>
              <p className="mt-1 text-sm text-slate-500 dark:text-slate-400 mb-6">
                Drift compliance scores, open drift counts, and AI Configuration Analyzer risk stats per device.
              </p>
              
              <div className="space-y-4">
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-xs font-medium text-slate-700 dark:text-slate-300 mb-1.5 uppercase tracking-wider">
                      Window
                    </label>
                    <select
                      value={complianceDays}
                      onChange={(e) => setComplianceDays(Number(e.target.value))}
                      className="w-full form-select focus:ring-indigo-500 focus:border-indigo-500 block sm:text-sm border-slate-300 dark:border-slate-700 rounded-md bg-white dark:bg-slate-950 dark:text-white"
                    >
                      <option value={7}>Last 7 Days</option>
                      <option value={30}>Last 30 Days</option>
                      <option value={90}>Last 90 Days</option>
                    </select>
                  </div>
                  
                  <div>
                    <label className="block text-xs font-medium text-slate-700 dark:text-slate-300 mb-1.5 uppercase tracking-wider">
                      Format
                    </label>
                    <select
                      value={complianceFormat}
                      onChange={(e) => setComplianceFormat(e.target.value)}
                      className="w-full form-select focus:ring-indigo-500 focus:border-indigo-500 block sm:text-sm border-slate-300 dark:border-slate-700 rounded-md bg-white dark:bg-slate-950 dark:text-white"
                    >
                      <option value="pdf">PDF Document</option>
                      <option value="csv">CSV Spreadsheet</option>
                    </select>
                  </div>
                </div>
              </div>
            </div>
          </div>
          <div className="px-6 py-4 bg-slate-50 dark:bg-slate-800/50 border-t border-slate-200 dark:border-slate-800 flex justify-end">
            <button
              onClick={handleDownloadCompliance}
              disabled={complianceDownloading}
              className="inline-flex items-center gap-2 px-4 py-2 bg-white dark:bg-slate-800 border border-slate-300 dark:border-slate-700 hover:bg-slate-50 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-200 text-sm font-medium rounded-md shadow-sm transition-colors disabled:opacity-50"
            >
              {complianceDownloading ? (
                <div className="animate-spin w-4 h-4 border-2 border-slate-300 border-t-indigo-600 rounded-full" />
              ) : (
                <Download className="w-4 h-4" />
              )}
              Download Compliance Report
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
