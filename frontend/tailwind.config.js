/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        risk: {
          normal: '#16a34a',
          adjust: '#d97706',
          high: '#dc2626',
        },
      },
    },
  },
  plugins: [],
}
