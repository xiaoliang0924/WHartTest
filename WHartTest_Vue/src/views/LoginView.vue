<template>
  <div class="login-page">
    <img
      class="login-bg"
      src="/login-bg-hd.png"
      srcset="/login-bg-hd.png 1024w, /login-bg-1920.png 1920w, /login-bg-2560.png 2560w"
      sizes="100vw"
      alt=""
      width="1920"
      height="1080"
      draggable="false"
      decoding="async"
      fetchpriority="high"
    />

    <div class="locale-switcher-shell">
      <AppLocaleToggle />
    </div>

    <div class="content-layer">
      <div class="login-card">
        <div class="card-header">
          <h2>{{ cardTitle }}</h2>
          <p>{{ cardDescription }}</p>
        </div>

        <form class="login-form" @submit.prevent="handleLogin">
          <div class="input-group">
            <div class="input-wrapper">
              <span class="input-icon" aria-hidden="true">
                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M15.75 6a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0zM4.501 20.118a7.5 7.5 0 0114.998 0A17.933 17.933 0 0112 21.75c-2.676 0-5.216-.584-7.499-1.632z" />
                </svg>
              </span>
              <input
                ref="usernameInputRef"
                v-model="username"
                type="text"
                required
                autocomplete="username"
                :placeholder="usernamePlaceholder"
                class="form-input"
              />
            </div>
          </div>

          <div class="input-group">
            <div class="input-wrapper">
              <span class="input-icon" aria-hidden="true">
                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M16.5 10.5V6.75a4.5 4.5 0 10-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H6.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z" />
                </svg>
              </span>
              <input
                v-model="password"
                :type="showPassword ? 'text' : 'password'"
                required
                autocomplete="current-password"
                :placeholder="passwordPlaceholder"
                class="form-input"
              />
              <button
                class="toggle-icon"
                type="button"
                :aria-label="showPassword ? t('login.hidePassword') : t('login.showPassword')"
                @click="showPassword = !showPassword"
              >
                <svg v-if="!showPassword" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M2.036 12.322a1.012 1.012 0 010-.639C3.423 7.51 7.36 4.5 12 4.5c4.638 0 8.573 3.007 9.963 7.178.07.207.07.431 0 .639C20.577 16.49 16.64 19.5 12 19.5c-4.638 0-8.573-3.007-9.963-7.178z" />
                  <path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                </svg>
                <svg v-else xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M3.98 8.223A10.477 10.477 0 001.934 12C3.226 16.338 7.244 19.5 12 19.5c.993 0 1.953-.138 2.863-.395M6.228 6.228A10.45 10.45 0 0112 4.5c4.756 0 8.773 3.162 10.065 7.498a10.523 10.523 0 01-4.293 5.774M6.228 6.228L3 3m3.228 3.228l3.65 3.65m7.894 7.894L21 21m-3.228-3.228l-3.65-3.65m0 0a3 3 0 10-4.243-4.243m4.242 4.242L9.88 9.88" />
                </svg>
              </button>
            </div>
          </div>

          <div class="form-footer">
            <label class="remember-me">
              <input v-model="rememberMe" type="checkbox" />
              <span>{{ rememberMeLabel }}</span>
            </label>
          </div>

          <button type="submit" class="login-btn" :disabled="isLoading">
            <template v-if="!isLoading">
              <span>{{ submitLabel }}</span>
              <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" aria-hidden="true">
                <path stroke-linecap="round" stroke-linejoin="round" d="M13.5 4.5L21 12m0 0l-7.5 7.5M21 12H3" />
              </svg>
            </template>
            <template v-else>
              <svg class="spinner" viewBox="0 0 24 24" fill="none">
                <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" opacity="0.25" />
                <path fill="currentColor" opacity="0.75" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
              </svg>
              {{ submittingLabel }}
            </template>
          </button>

          <div v-if="errorMessage" class="error-msg">{{ errorMessage }}</div>

          <p class="register-link">
            {{ registerPrompt }}
            <router-link to="/register">{{ registerLinkLabel }}</router-link>
          </p>
        </form>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { Message } from '@arco-design/web-vue'
import { useRouter } from 'vue-router'
import { useAppI18n } from '@/composables/useAppI18n'
import { useAuthStore } from '@/store/authStore'
import { useSystemConfigStore } from '@/store/systemConfigStore'
import AppLocaleToggle from '@/components/AppLocaleToggle.vue'

const usernameInputRef = ref<HTMLInputElement | null>(null)
const username = ref('')
const password = ref('')
const rememberMe = ref(false)
const showPassword = ref(false)

const router = useRouter()
const authStore = useAuthStore()
const systemConfigStore = useSystemConfigStore()
const { isEnglish, t } = useAppI18n()
const isLoading = computed(() => authStore.getIsLoading)
const errorMessage = computed(() => authStore.getLoginError)

const usernamePlaceholder = computed(() => t('register.usernamePlaceholder'))
const passwordPlaceholder = computed(() => t('register.passwordPlaceholder'))
const cardTitle = computed(() => {
  const configured = systemConfigStore.getLoginTitle?.trim()
  if (configured) return configured
  return isEnglish.value ? 'Automated Testing Platform' : '自动化测试平台'
})
const cardDescription = computed(() => {
  const configured = systemConfigStore.getLoginSubtitle?.trim()
  if (configured) return configured
  return isEnglish.value ? 'Sign in to start efficient testing' : '欢迎登录，开启高效测试之旅'
})
const rememberMeLabel = computed(() => (
  isEnglish.value ? 'Remember me' : '记住我'
))
const submitLabel = computed(() => (
  isEnglish.value ? 'Sign in' : '登录'
))
const submittingLabel = computed(() => (
  isEnglish.value ? 'Signing in...' : '登录中...'
))
const registerPrompt = computed(() => (
  isEnglish.value ? 'No account yet?' : '还没有账号?'
))
const registerLinkLabel = computed(() => (
  isEnglish.value ? 'Register now' : '立即注册'
))
const loginRequiredMessage = computed(() => (
  isEnglish.value ? 'Enter username and password' : '请输入用户名和密码'
))
const loginSuccessMessage = computed(() => (
  isEnglish.value ? 'Signed in successfully!' : '登录成功！'
))

const focusUsernameInput = async () => {
  await nextTick()
  usernameInputRef.value?.focus()
}

const handleLogin = async () => {
  if (!username.value || !password.value) {
    Message.warning(loginRequiredMessage.value)
    return
  }

  const success = await authStore.login(username.value, password.value)
  if (success) {
    Message.success(loginSuccessMessage.value)
    if (rememberMe.value) {
      localStorage.setItem('rememberedUsername', username.value)
    } else {
      localStorage.removeItem('rememberedUsername')
    }
    await router.push({ name: 'Dashboard' })
  }
}

onMounted(async () => {
  await systemConfigStore.fetchConfig()
  authStore.checkAuthStatus()
  if (authStore.isLoggedIn) {
    router.push({ name: 'Dashboard' })
  }
  const saved = localStorage.getItem('rememberedUsername')
  if (saved) {
    username.value = saved
    rememberMe.value = true
  }
  focusUsernameInput()
})
</script>

<style scoped>
.login-page {
  position: relative;
  min-height: 100vh;
  overflow: hidden;
  background: #b9e0fd;
}

.login-bg {
  position: absolute;
  inset: 0;
  z-index: 0;
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
  object-position: left center;
  pointer-events: none;
  user-select: none;
}

.locale-switcher-shell {
  position: absolute;
  top: 24px;
  right: 24px;
  z-index: 3;
}

.content-layer {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  min-height: 100vh;
  padding: 32px clamp(24px, 7vw, 100px);
}

.login-card {
  width: min(100%, 420px);
  padding: 36px 34px 30px;
  border: 1px solid rgba(255, 255, 255, 0.95);
  border-radius: 20px;
  background: #fff;
  box-shadow:
    0 20px 50px rgba(15, 76, 129, 0.14),
    0 6px 18px rgba(15, 76, 129, 0.08);
  animation: fade-in-up 0.6s ease-out;
}

.card-header {
  margin-bottom: 28px;
  text-align: left;
}

.card-header h2 {
  margin: 0 0 8px;
  color: #0f172a;
  font-size: 28px;
  font-weight: 700;
  letter-spacing: -0.4px;
}

.card-header p {
  margin: 0;
  color: #64748b;
  font-size: 14px;
  line-height: 1.6;
}

.login-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.input-group {
  width: 100%;
}

.input-wrapper {
  position: relative;
  display: flex;
  align-items: center;
}

.input-icon {
  position: absolute;
  left: 14px;
  z-index: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: #94a3b8;
  pointer-events: none;
}

.input-icon svg {
  width: 18px;
  height: 18px;
}

.form-input {
  box-sizing: border-box;
  width: 100%;
  padding: 13px 42px 13px 44px;
  border: 1px solid #dbeafe;
  border-radius: 12px;
  background: #f8fbff;
  color: #0f172a;
  font-size: 14px;
  outline: none;
  transition: all 0.25s ease;
}

.form-input::placeholder {
  color: #94a3b8;
}

.form-input:focus {
  border-color: #3b82f6;
  background: #fff;
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.12);
}

.form-input::-ms-clear,
.form-input::-ms-reveal {
  display: none;
}

.toggle-icon {
  position: absolute;
  right: 12px;
  top: 50%;
  transform: translateY(-50%);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 4px;
  border: none;
  background: transparent;
  color: #94a3b8;
  cursor: pointer;
  transition: color 0.2s ease;
}

.toggle-icon:hover {
  color: #64748b;
}

.toggle-icon svg {
  width: 18px;
  height: 18px;
}

.form-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: -2px;
}

.remember-me {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: #64748b;
  cursor: pointer;
  user-select: none;
}

.remember-me input {
  accent-color: #2563eb;
}

.login-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  width: 100%;
  padding: 13px;
  border: none;
  border-radius: 12px;
  background: linear-gradient(135deg, #2563eb, #1d4ed8);
  box-shadow: 0 8px 20px rgba(37, 99, 235, 0.28);
  color: #fff;
  cursor: pointer;
  font-size: 15px;
  font-weight: 600;
  transition: all 0.25s ease;
}

.login-btn svg {
  width: 18px;
  height: 18px;
}

.login-btn:hover:not(:disabled) {
  transform: translateY(-1px);
  background: linear-gradient(135deg, #3b82f6, #2563eb);
  box-shadow: 0 10px 24px rgba(37, 99, 235, 0.34);
}

.login-btn:disabled {
  opacity: 0.65;
  cursor: not-allowed;
  transform: none;
}

.spinner {
  width: 18px;
  height: 18px;
  animation: spin 1s linear infinite;
}

.error-msg {
  padding: 10px 14px;
  border: 1px solid rgba(239, 68, 68, 0.25);
  border-radius: 10px;
  background: rgba(254, 226, 226, 0.85);
  color: #dc2626;
  font-size: 13px;
  text-align: center;
}

.register-link {
  margin: 4px 0 0;
  font-size: 13px;
  text-align: center;
  color: #64748b;
}

.register-link a {
  color: #2563eb;
  font-weight: 600;
  text-decoration: none;
  transition: color 0.2s ease;
}

.register-link a:hover {
  color: #1d4ed8;
  text-decoration: underline;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

@keyframes fade-in-up {
  from {
    opacity: 0;
    transform: translateY(20px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@media (max-width: 960px) {
  .content-layer {
    justify-content: center;
    padding: 24px 16px;
  }

  .login-bg {
    object-position: 38% center;
  }
}

@media (max-width: 640px) {
  .login-card {
    padding: 28px 22px 24px;
  }

  .card-header h2 {
    font-size: 24px;
  }
}
</style>
