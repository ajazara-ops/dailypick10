import { theme } from '../theme';
import React from 'react';
import { View, Text, TouchableOpacity } from 'react-native';
import { DELETION_DISABLED_MESSAGE, type DeletionState } from '../domain/deletion';

export const deletionMessage = (state: DeletionState): string => state.message || ({
  disabled: DELETION_DISABLED_MESSAGE,
  loading: '이전 탈퇴 요청을 확인하고 있습니다.', idle: '',
  reauthenticating: '탈퇴 요청 전 본인을 확인합니다.', submitting: '탈퇴 요청 접수를 확인하고 있습니다.',
  grace_period: '탈퇴 신청이 접수되었습니다. 신청 서버시각부터 7일 유예 중이며 일반 서비스와 추천 알림은 중지됩니다. 재로그인만으로 취소되지 않습니다.',
  queued: '7일 유예가 끝났습니다. 서버 작업 실행을 기다리고 있으며 아직 삭제 완료가 아닙니다. 취소할 수 없습니다.',
  cancel_requested: '취소 요청을 접수해 삭제를 보류했습니다. 서버의 본인 확인을 기다리며 일반 서비스와 추천 알림은 아직 중지 상태입니다.',
  cancelled: '본인 확인 후 탈퇴 신청을 취소했습니다. 일반 서비스를 다시 이용할 수 있습니다.',
  processing: '삭제 실행이 시작되었습니다. 복구할 수 없으며 아직 정리 완료는 아닙니다.',
  retry_wait: '일부 정리가 끝나지 않아 서버 재시도를 기다립니다. 요청을 다시 만들 필요는 없습니다.',
  needs_review: '자동 정리를 완료하지 못했습니다. 담당자의 확인이 필요합니다.',
  unknown: '접수 또는 완료 여부를 확인하지 못했습니다. 상태 확인으로 기존 요청을 조회하세요.',
  not_found: '서버에서 접수된 요청을 찾지 못했습니다. 같은 계정에서 본인 확인 후 다시 요청할 수 있습니다.',
  completed: '로그인 계정과 앱 사용자 문서의 정리가 완료되었습니다. 외부 서비스 기록과 스토어 구독은 별도입니다.',
}[state.status]);

export function DeletionStatusPanel({state, onCheck, onCancel}: {state: DeletionState; onCheck: () => void; onCancel: () => void}) {
  if (['idle', 'disabled'].includes(state.status)) return null;
  return <View accessibilityLiveRegion="polite" style={{padding:12,backgroundColor:theme.surface}}>
    <Text style={{color:theme.accent}}>{state.mode === 'mock' ? '모의 탈퇴 처리 · ' : state.mode === 'emulator' ? '로컬 시험 서버 · 합성 계정 · ' : ''}{deletionMessage(state)}</Text>
    {state.receipt && <Text style={{color:theme.secondary,marginTop:6}}>신청: {new Date(state.receipt.requestedAt*1000).toISOString()} / 유예 종료: {new Date(state.receipt.deleteAfter*1000).toISOString()} (UTC, 서버 기준)</Text>}
    <Text style={{color:theme.secondary,marginTop:6}}>회원 탈퇴만으로 스토어 구독이 자동 해지되거나 환불되지 않습니다.</Text>
    {state.status === 'grace_period' && state.receipt?.cancelAllowed && <TouchableOpacity accessibilityRole="button" onPress={onCancel} style={{padding:10}}><Text style={{color:theme.accent}}>본인 확인 후 탈퇴 신청 취소</Text></TouchableOpacity>}
    {!['loading','reauthenticating','submitting'].includes(state.status) && <TouchableOpacity accessibilityRole="button" onPress={onCheck} style={{padding:10}}><Text style={{color:theme.accent}}>처리 상태 확인</Text></TouchableOpacity>}
  </View>;
}
