pipeline {
    agent none
    options {
        disableConcurrentBuilds()
        timeout(time: 20, unit: 'MINUTES')
    }
    stages {
        stage('Validate') {
            agent { label 'build-agent' }
            stages {
                stage('Prepare validation image') {
                    steps {
                        script {
                            env.VALIDATION_IMAGE = "lightnow-proxy:ci-${env.BUILD_TAG.replaceAll(/[^A-Za-z0-9_.-]/, '-').take(96)}"
                        }
                    }
                }
                stage('Build runtime') {
                    steps {
                        sh 'docker build -t "${VALIDATION_IMAGE}" .'
                    }
                }
                stage('Runtime smoke') {
                    steps {
                        sh 'docker run --rm "${VALIDATION_IMAGE}" lightnow-proxy --help'
                    }
                }
            }
            post {
                always {
                    script {
                        if (env.VALIDATION_IMAGE) {
                            sh 'docker image rm "${VALIDATION_IMAGE}" >/dev/null 2>&1 || true'
                        }
                    }
                }
            }
        }
    }
}
