import type { ThemeConfig } from 'antd';

export const workspaceTheme: ThemeConfig = {
  token: {
    colorPrimary: '#5750c7', colorInfo: '#5750c7', colorSuccess: '#267556',
    colorWarning: '#986116', colorError: '#bb3f42', colorText: '#252936',
    colorTextSecondary: '#646a7b', colorBgLayout: '#f5f6f9', colorBorder: '#e0e3ec',
    borderRadius: 10, controlHeight: 38, fontSize: 14,
    fontFamily: '"Inter", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
  },
  components: {
    Button: { primaryShadow: 'none' },
    Layout: { headerBg: '#ffffff', siderBg: '#f8f9fc', bodyBg: '#ffffff' },
    Tabs: { horizontalItemGutter: 24 },
    Table: { headerBg: '#f7f8fb', cellPaddingBlockSM: 10, cellPaddingInlineSM: 12 },
  },
};
