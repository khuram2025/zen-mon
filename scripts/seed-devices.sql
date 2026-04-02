-- Seed test devices for development
-- Run after init-postgres.sql

-- Get the Core Network group ID
DO $$
DECLARE
    core_group_id UUID;
    dist_group_id UUID;
    access_group_id UUID;
    server_group_id UUID;
    dmz_group_id UUID;
    admin_id UUID;
BEGIN
    SELECT id INTO core_group_id FROM device_groups WHERE name = 'Core Network';
    SELECT id INTO dist_group_id FROM device_groups WHERE name = 'Distribution';
    SELECT id INTO access_group_id FROM device_groups WHERE name = 'Access Layer';
    SELECT id INTO server_group_id FROM device_groups WHERE name = 'Servers';
    SELECT id INTO dmz_group_id FROM device_groups WHERE name = 'DMZ';
    SELECT id INTO admin_id FROM users WHERE username = 'admin';

    -- Core Network devices
    INSERT INTO devices (hostname, ip_address, device_type, location, group_id, ping_interval, description, created_by) VALUES
        ('core-router-01', '10.0.0.1', 'router', 'DC-1 Rack A1', core_group_id, 30, 'Primary core router', admin_id),
        ('core-router-02', '10.0.0.2', 'router', 'DC-1 Rack A2', core_group_id, 30, 'Secondary core router', admin_id),
        ('core-switch-01', '10.0.0.3', 'switch', 'DC-1 Rack B1', core_group_id, 30, 'Core L3 switch', admin_id),
        ('core-switch-02', '10.0.0.4', 'switch', 'DC-1 Rack B2', core_group_id, 30, 'Core L3 switch redundant', admin_id);

    -- Distribution layer
    INSERT INTO devices (hostname, ip_address, device_type, location, group_id, ping_interval, description, created_by) VALUES
        ('dist-switch-floor1', '10.0.1.1', 'switch', 'Building A Floor 1', dist_group_id, 60, 'Floor 1 distribution switch', admin_id),
        ('dist-switch-floor2', '10.0.1.2', 'switch', 'Building A Floor 2', dist_group_id, 60, 'Floor 2 distribution switch', admin_id),
        ('dist-switch-floor3', '10.0.1.3', 'switch', 'Building A Floor 3', dist_group_id, 60, 'Floor 3 distribution switch', admin_id),
        ('dist-switch-bldgb', '10.0.1.4', 'switch', 'Building B Floor 1', dist_group_id, 60, 'Building B distribution switch', admin_id);

    -- Access layer
    INSERT INTO devices (hostname, ip_address, device_type, location, group_id, ping_interval, description, created_by) VALUES
        ('ap-lobby-01', '10.0.2.1', 'access_point', 'Main Lobby', access_group_id, 60, 'Lobby wireless AP', admin_id),
        ('ap-conf-01', '10.0.2.2', 'access_point', 'Conference Room A', access_group_id, 60, 'Conference room AP', admin_id),
        ('ap-office-01', '10.0.2.3', 'access_point', 'Open Office Area', access_group_id, 60, 'Office AP 1', admin_id),
        ('ap-office-02', '10.0.2.4', 'access_point', 'Open Office Area', access_group_id, 60, 'Office AP 2', admin_id),
        ('printer-floor1', '10.0.2.10', 'printer', 'Building A Floor 1', access_group_id, 120, 'Floor 1 network printer', admin_id),
        ('printer-floor2', '10.0.2.11', 'printer', 'Building A Floor 2', access_group_id, 120, 'Floor 2 network printer', admin_id);

    -- Servers
    INSERT INTO devices (hostname, ip_address, device_type, location, group_id, ping_interval, description, created_by) VALUES
        ('web-server-01', '10.0.10.1', 'server', 'DC-1 Rack C1', server_group_id, 30, 'Production web server 1', admin_id),
        ('web-server-02', '10.0.10.2', 'server', 'DC-1 Rack C2', server_group_id, 30, 'Production web server 2', admin_id),
        ('db-server-01', '10.0.10.10', 'server', 'DC-1 Rack D1', server_group_id, 30, 'Primary database server', admin_id),
        ('db-server-02', '10.0.10.11', 'server', 'DC-1 Rack D2', server_group_id, 30, 'Replica database server', admin_id),
        ('app-server-01', '10.0.10.20', 'server', 'DC-1 Rack C3', server_group_id, 30, 'Application server', admin_id),
        ('monitoring-server', '10.0.10.30', 'server', 'DC-1 Rack E1', server_group_id, 30, 'Monitoring infrastructure', admin_id);

    -- DMZ
    INSERT INTO devices (hostname, ip_address, device_type, location, group_id, ping_interval, description, created_by) VALUES
        ('firewall-ext', '10.0.0.10', 'firewall', 'DC-1 Rack A3', dmz_group_id, 30, 'External firewall', admin_id),
        ('firewall-int', '10.0.0.11', 'firewall', 'DC-1 Rack A4', dmz_group_id, 30, 'Internal firewall', admin_id),
        ('vpn-gateway', '10.0.0.20', 'router', 'DC-1 Rack A5', dmz_group_id, 60, 'VPN concentrator', admin_id),
        ('mail-server', '10.0.0.30', 'server', 'DC-1 Rack F1', dmz_group_id, 60, 'Mail server', admin_id);

    -- Also add some public reachable targets for testing
    INSERT INTO devices (hostname, ip_address, device_type, location, group_id, ping_interval, description, created_by) VALUES
        ('google-dns', '8.8.8.8', 'other', 'Internet', NULL, 60, 'Google Public DNS - internet connectivity check', admin_id),
        ('cloudflare-dns', '1.1.1.1', 'other', 'Internet', NULL, 60, 'Cloudflare DNS - internet connectivity check', admin_id),
        ('quad9-dns', '9.9.9.9', 'other', 'Internet', NULL, 60, 'Quad9 DNS - internet connectivity check', admin_id);

    -- Create default alert rules
    INSERT INTO alert_rules (name, description, metric, operator, threshold, duration, severity, cooldown, created_by) VALUES
        ('Device Down', 'Alert when any device goes down', 'ping_status', 'eq', 0, 180, 'critical', 300, admin_id),
        ('High Latency', 'Alert when RTT exceeds 100ms', 'rtt', 'gt', 100, 300, 'warning', 600, admin_id),
        ('Packet Loss', 'Alert when packet loss exceeds 5%', 'packet_loss', 'gt', 5, 300, 'warning', 600, admin_id);

END $$;
