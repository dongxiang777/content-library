//go:build darwin

package system

import (
	"fmt"
	"os/exec"
	"regexp"
	"strings"
)

func enable_proxy(args ProxySettings) error {
	args = merge_default_settings(args)
	if strings.TrimSpace(args.PACURL) != "" {
		return enable_pac_proxy(args)
	}
	// Manual mode: clear PAC so it does not compete with HTTP/HTTPS proxy.
	_ = set_auto_proxy_state(args.Device, false)
	cmd1 := exec.Command("networksetup", "-setwebproxy", args.Device, args.Hostname, args.Port)
	_, err1 := cmd1.Output()
	if err1 != nil {
		return fmt.Errorf("设置 HTTP 代理失败，%v", err1.Error())
	}
	cmd2 := exec.Command("networksetup", "-setsecurewebproxy", args.Device, args.Hostname, args.Port)
	output, err2 := cmd2.Output()
	if err2 != nil {
		return fmt.Errorf("设置 HTTPS 代理失败，%v", output)
	}
	return nil
}

// enable_pac_proxy installs a selective PAC and turns off blanket HTTP/HTTPS/SOCKS
// system proxies so Chrome and other apps are not forced through the MITM port.
func enable_pac_proxy(args ProxySettings) error {
	args = merge_default_settings(args)
	pacURL := strings.TrimSpace(args.PACURL)
	if pacURL == "" {
		return fmt.Errorf("PAC URL 为空")
	}

	// Disable manual proxies first — otherwise they take precedence over PAC on
	// some macOS versions / apps, and Clash/Vortex may leave SOCKS enabled.
	if err := set_web_proxy_state(args.Device, false); err != nil {
		return err
	}
	if err := set_secure_web_proxy_state(args.Device, false); err != nil {
		return err
	}
	_ = set_socks_proxy_state(args.Device, false)

	cmd := exec.Command("networksetup", "-setautoproxyurl", args.Device, pacURL)
	if output, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("设置 PAC 失败，%v: %s", err, strings.TrimSpace(string(output)))
	}
	if err := set_auto_proxy_state(args.Device, true); err != nil {
		return err
	}
	return nil
}

func disable_proxy(args ProxySettings) error {
	args = merge_default_settings(args)
	var errs []string
	if err := set_web_proxy_state(args.Device, false); err != nil {
		errs = append(errs, err.Error())
	}
	if err := set_secure_web_proxy_state(args.Device, false); err != nil {
		errs = append(errs, err.Error())
	}
	_ = set_socks_proxy_state(args.Device, false)
	if err := set_auto_proxy_state(args.Device, false); err != nil {
		errs = append(errs, err.Error())
	}
	// Clear PAC URL so a later re-enable of auto-proxy by another app does not
	// accidentally re-point at our (now dead) endpoint.
	_ = exec.Command("networksetup", "-setautoproxyurl", args.Device, "").Run()
	if len(errs) > 0 {
		return fmt.Errorf("禁用系统代理失败: %s", strings.Join(errs, "; "))
	}
	return nil
}

func fetch_cur_proxy(args ProxySettings) (*ProxySettings, error) {
	device := args.Device
	if device == "" {
		if port, err := get_network_interfaces(); err == nil && port != nil {
			device = port.Port
		}
	}
	if device == "" {
		device = "Wi-Fi"
	}

	// Prefer PAC when enabled — that is our selective-proxy mode.
	if pacURL, enabled, err := read_auto_proxy(device); err == nil && enabled && pacURL != "" {
		return &ProxySettings{
			Device: device,
			PACURL: pacURL,
		}, nil
	}

	webProxy, err := read_network_proxy(device, false)
	if err != nil {
		return nil, err
	}
	if webProxy.Enabled && webProxy.Server != "" && webProxy.Port != "" {
		return &ProxySettings{
			Device:   device,
			Hostname: webProxy.Server,
			Port:     webProxy.Port,
		}, nil
	}
	secureProxy, err := read_network_proxy(device, true)
	if err != nil {
		return nil, err
	}
	if secureProxy.Enabled && secureProxy.Server != "" && secureProxy.Port != "" {
		return &ProxySettings{
			Device:   device,
			Hostname: secureProxy.Server,
			Port:     secureProxy.Port,
		}, nil
	}
	return nil, nil
}

type network_proxy_info struct {
	Enabled bool
	Server  string
	Port    string
}

func read_network_proxy(device string, secure bool) (*network_proxy_info, error) {
	command := "-getwebproxy"
	if secure {
		command = "-getsecurewebproxy"
	}
	output, err := exec.Command("networksetup", command, device).Output()
	if err != nil {
		return nil, fmt.Errorf("读取系统代理失败，%v", err)
	}
	info := &network_proxy_info{}
	lines := strings.Split(string(output), "\n")
	for _, line := range lines {
		parts := strings.SplitN(strings.TrimSpace(line), ":", 2)
		if len(parts) != 2 {
			continue
		}
		key := strings.ToLower(strings.TrimSpace(parts[0]))
		value := strings.TrimSpace(parts[1])
		switch key {
		case "enabled":
			info.Enabled = strings.EqualFold(value, "yes")
		case "server":
			info.Server = value
		case "port":
			info.Port = value
		}
	}
	return info, nil
}

func read_auto_proxy(device string) (string, bool, error) {
	output, err := exec.Command("networksetup", "-getautoproxyurl", device).Output()
	if err != nil {
		return "", false, fmt.Errorf("读取 PAC 失败，%v", err)
	}
	url := ""
	enabled := false
	for _, line := range strings.Split(string(output), "\n") {
		parts := strings.SplitN(strings.TrimSpace(line), ":", 2)
		if len(parts) != 2 {
			continue
		}
		key := strings.ToLower(strings.TrimSpace(parts[0]))
		value := strings.TrimSpace(parts[1])
		switch key {
		case "url":
			if !strings.EqualFold(value, "(null)") && value != "" {
				url = value
			}
		case "enabled":
			enabled = strings.EqualFold(value, "yes")
		}
	}
	return url, enabled, nil
}

func set_web_proxy_state(device string, on bool) error {
	state := "off"
	if on {
		state = "on"
	}
	if output, err := exec.Command("networksetup", "-setwebproxystate", device, state).CombinedOutput(); err != nil {
		return fmt.Errorf("设置 HTTP 代理开关失败，%v: %s", err, strings.TrimSpace(string(output)))
	}
	return nil
}

func set_secure_web_proxy_state(device string, on bool) error {
	state := "off"
	if on {
		state = "on"
	}
	if output, err := exec.Command("networksetup", "-setsecurewebproxystate", device, state).CombinedOutput(); err != nil {
		return fmt.Errorf("设置 HTTPS 代理开关失败，%v: %s", err, strings.TrimSpace(string(output)))
	}
	return nil
}

func set_socks_proxy_state(device string, on bool) error {
	state := "off"
	if on {
		state = "on"
	}
	if output, err := exec.Command("networksetup", "-setsocksfirewallproxystate", device, state).CombinedOutput(); err != nil {
		return fmt.Errorf("设置 SOCKS 代理开关失败，%v: %s", err, strings.TrimSpace(string(output)))
	}
	return nil
}

func set_auto_proxy_state(device string, on bool) error {
	state := "off"
	if on {
		state = "on"
	}
	if output, err := exec.Command("networksetup", "-setautoproxystate", device, state).CombinedOutput(); err != nil {
		return fmt.Errorf("设置 PAC 开关失败，%v: %s", err, strings.TrimSpace(string(output)))
	}
	return nil
}

func get_network_interfaces() (*HardwarePort, error) {
	// 获取所有硬件端口信息
	cmd := exec.Command("networksetup", "-listallhardwareports")
	output, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("执行 networksetup 命令失败: %v", err)
	}
	// 解析硬件端口信息
	var ports []HardwarePort
	lines := strings.Split(string(output), "\n")

	var cur_port HardwarePort
	for _, line := range lines {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "Hardware Port:") {
			if cur_port.Port != "" {
				ports = append(ports, cur_port)
			}
			cur_port = HardwarePort{}
			cur_port.Port = strings.TrimPrefix(line, "Hardware Port: ")
		} else if strings.HasPrefix(line, "Device:") {
			cur_port.Device = strings.TrimPrefix(line, "Device: ")
		}
	}
	if cur_port.Port != "" {
		ports = append(ports, cur_port)
	}
	// 获取网络接口信息
	cmd = exec.Command("scutil", "--nwi")
	output, err = cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("执行 scutil 命令失败: %v", err)
	}
	// 使用正则解析接口信息
	re := regexp.MustCompile(`Network interfaces{0,1}: ([0-9a-zA-Z]{1,})`)
	matches := re.FindStringSubmatch(string(output))
	// 将接口信息与硬件端口匹配
	if len(matches) >= 2 {
		for i := range ports {
			if ports[i].Device == matches[1] {
				return &ports[i], nil
			}
		}
	}
	return nil, fmt.Errorf("未找到硬件端口信息")
}
