pipeline {
    agent { label 'container-build-amd64' }
    options {
        timestamps()
        disableConcurrentBuilds()
        timeout(time: 20, unit: 'MINUTES')
    }
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
        stage('Lint and test') {
            steps {
                sh '''
                    docker run --rm --user "$(id -u):$(id -g)" \
                        -e HOME=/tmp \
                        -e UV_PROJECT_ENVIRONMENT=/tmp/proxy-ci-venv \
                        -e UV_CACHE_DIR=/tmp/uv-cache \
                        -v "$PWD:/workspace:ro" -w /workspace \
                        "${VALIDATION_IMAGE}" sh -ec '
                            mkdir /tmp/source
                            tar --exclude=.git --exclude=.venv --exclude=.pytest_cache --exclude=.ruff_cache -C /workspace -cf - . | tar -C /tmp/source -xf -
                            cd /tmp/source
                            uv run --extra dev ruff check src tests examples --no-cache
                            uv run --extra dev pytest -q -o cache_dir=/tmp/pytest-cache
                        '
                '''
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
            sh 'if test -n "${VALIDATION_IMAGE:-}"; then docker image rm "${VALIDATION_IMAGE}" >/dev/null 2>&1 || true; fi'
        }
    }
}
